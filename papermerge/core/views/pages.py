import io
import os
import logging
from pikepdf import Pdf

from django.http import Http404

from rest_framework.generics import (
    RetrieveAPIView,
    DestroyAPIView,
    GenericAPIView
)

from rest_framework import status
from rest_framework.response import Response
from rest_framework_json_api.renderers import JSONRenderer
from rest_framework.parsers import JSONParser

from drf_spectacular.utils import extend_schema

from papermerge.core.models import Page, Document, Folder
from papermerge.core.lib.utils import (
    get_assigns_after_delete,
    get_reordered_list,
    annotate_page_data
)
from papermerge.core.lib.path import PagePath
from papermerge.core.storage import abs_path, get_storage_instance
from papermerge.core.serializers import (
    PageSerializer,
    PageDeleteSerializer,
    PagesReorderSerializer,
    PagesRotateSerializer,
    PagesMoveToDocumentSerializer,
    PagesMoveToFolderSerializer
)
from papermerge.core.renderers import (
    PlainTextRenderer,
    ImageJpegRenderer,
    ImageSVGRenderer
)
from papermerge.core.exceptions import APIBadRequest
from .mixins import RequireAuthMixin
from ..models.utils import OCR_STATUS_SUCCEEDED

logger = logging.getLogger(__name__)


def copy_pages_data(old_version, new_version, pages_map):
    for src_page_number, dst_page_number in pages_map:
        src_page_path = PagePath(
            document_path=old_version.document_path,
            page_num=src_page_number
        )
        dst_page_path = PagePath(
            document_path=new_version.document_path,
            page_num=dst_page_number
        )
        get_storage_instance().copy_page(src=src_page_path, dst=dst_page_path)


def reuse_ocr_data(old_version, new_version, page_map):
    for new_number, old_number in page_map:
        src_page_path = PagePath(
            document_path=old_version.document_path,
            page_num=old_number
        )
        dst_page_path = PagePath(
            document_path=new_version.document_path,
            page_num=new_number
        )
        get_storage_instance().copy_page(
            src=src_page_path,
            dst=dst_page_path
        )


def collect_text_streams(version, page_numbers):
    pages_map = {page.number: page for page in version.pages.all()}

    streams = []
    for number in page_numbers:
        stream = io.StringIO(pages_map[number].text)
        streams.append(stream)

    return streams


def reuse_text_field(old_version, new_version, page_map):
    streams = collect_text_streams(
        version=old_version,
        # list of old_version page numbers
        page_numbers=[item[1] for item in page_map]
    )

    # updates page.text fields and document_version.text field
    new_version.update_text_field(streams)


def reuse_text_field_multi(
  src_old_version,
  dst_old_version,
  dst_new_version,
  position,
  pages
):
    page_map = [(pos, pos) for pos in range(1, position + 1)]
    streams = []
    if len(page_map) > 0:
        streams.extend(
            collect_text_streams(
                version=dst_old_version,
                page_numbers=[item[1] for item in page_map]
            )
        )

    page_map = zip(
        [pos for pos in range(position + 1, pages.count() + 1)],
        [page.number for page in pages.all()]
    )
    streams.extend(
        collect_text_streams(
            version=src_old_version,
            page_numbers=[item[1] for item in page_map]
        )
    )

    dst_new_total_pages = dst_new_version.pages.count()
    _range = range(
            position + 1 + pages.count(),
            dst_new_total_pages + 1
        )
    page_map = [(pos, pos - position - pages.count()) for pos in _range]
    streams.extend(
       collect_text_streams(
            version=dst_old_version,
            page_numbers=[item[1] for item in page_map]
       )
    )

    dst_new_version.update_text_field(streams)


def insert_pdf_pages(
    src_old_version,
    dst_old_version,
    dst_new_version,
    page_numbers,
    position
):
    src_old_pdf = Pdf.open(
        abs_path(src_old_version.document_path.url)
    )
    dst_old_pdf = Pdf.open(
        abs_path(dst_old_version.document_path.url)
    )

    _inserted_count = 0
    for page_number in page_numbers:
        pdf_page = src_old_pdf.pages.p(page_number)
        dst_old_pdf.pages.insert(position + _inserted_count, pdf_page)
        _inserted_count += 1

    dirname = os.path.dirname(
        abs_path(dst_new_version.document_path.url)
    )
    os.makedirs(dirname, exist_ok=True)
    dst_old_pdf.save(
        abs_path(dst_new_version.document_path.url)
    )


def remove_pdf_pages(old_version, new_version, pages_to_delete):
    """
    :param old_version: is instance of DocumentVersion
    :param new_version:  is instance of DocumentVersion
    :param pages_to_delete: queryset of pages to delete
    """
    # delete page from document's new version associated file
    pdf = Pdf.open(
        abs_path(old_version.document_path.url)
    )
    _deleted_count = 0
    for page in pages_to_delete:
        pdf.pages.remove(p=page.number - _deleted_count)
        _deleted_count += 1

    dirname = os.path.dirname(
        abs_path(new_version.document_path.url)
    )
    os.makedirs(dirname, exist_ok=True)
    pdf.save(abs_path(new_version.document_path.url))


def reorder_pdf_pages(
    old_version,
    new_version,
    pages_data,
    page_count
):
    src = Pdf.open(abs_path(old_version.document_path.url))

    dst = Pdf.new()
    reodered_list = sorted(pages_data, key=lambda item: item['new_number'])

    for list_item in reodered_list:
        page = src.pages.p(list_item['old_number'])
        dst.pages.append(page)

    dirname = os.path.dirname(
        abs_path(new_version.document_path.url)
    )
    os.makedirs(dirname, exist_ok=True)
    dst.save(abs_path(new_version.document_path.url))


def rotate_pdf_pages(
    old_version,
    new_version,
    pages_data
):
    """
    ``pages`` data is a list of dictionaries. Each dictionary is expected
    to have following keys:
        - number
        - angle
    """
    src = Pdf.open(abs_path(old_version.document_path.url))

    for page_data in pages_data:
        page = src.pages.p(page_data['number'])
        page.rotate(page_data['angle'], relative=True)

    dirname = os.path.dirname(
        abs_path(new_version.document_path.url)
    )
    os.makedirs(dirname, exist_ok=True)
    src.save(abs_path(new_version.document_path.url))


def reuse_ocr_data_after_rotate(
    old_version,
    new_version,
    pages_data,
    pages_count
):
    pass


class PageView(RequireAuthMixin, RetrieveAPIView, DestroyAPIView):
    serializer_class = PageSerializer
    renderer_classes = [
        PlainTextRenderer,
        ImageSVGRenderer,
        JSONRenderer,
        ImageJpegRenderer,
    ]

    def get_queryset(self, *args, **kwargs):
        # This is workaround warning issued when runnnig
        # `./manage.py generateschema`
        # https://github.com/carltongibson/django-filter/issues/966
        if not self.request:
            return Page.objects.none()

        return Page.objects.filter(
            document_version__document__user=self.request.user
        )

    @extend_schema(operation_id="Retrieve")
    def get(self, request, *args, **kwargs):
        """
        Retrieves page resource
        """
        return self.retrieve(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """Returns page as json, txt, jpeg or svg image
        """
        instance = self.get_object()

        # as plain text
        if request.accepted_renderer.format == 'txt':
            data = instance.text
            return Response(data)

        # as html
        if request.accepted_renderer.format in ('html', 'jpeg', 'jpg'):
            try:
                jpeg_data = instance.get_jpeg()
            except IOError as exc:
                logger.error(exc)
                raise Http404("Jpeg image not available")
            return Response(jpeg_data)

        # as svg (which includes embedded jpeg and HOCRed text overlay)
        if request.accepted_renderer.format == 'svg':
            content_type = 'image/svg+xml'
            try:
                data = instance.get_svg()
            except IOError:
                # svg not available, try jpeg
                try:
                    data = instance.get_jpeg()
                    content_type = 'image/jpeg'
                except IOError as exc:
                    logger.error(exc)
                    raise Http404("Neither JPEG nor SVG image not available")
            return Response(
                data,
                # either image/jpeg or image/svg+xml
                content_type=content_type
            )

        # by default render page with json serializer
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @extend_schema(operation_id="Single page delete")
    def delete(self, request, *args, **kwargs):
        """
        Deletes page resource
        """
        return self.destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        """
        Creates a new document version and copies
        all existing pages to it except current page.
        """
        if instance.is_archived:
            raise APIBadRequest(detail='Deleting archived page is not allowed')

        pages_to_delete = Page.objects.filter(pk__in=[instance.pk])
        old_version = instance.document_version
        doc = instance.document_version.document

        # create new version models i.e. DocumentVersion with Page(s)
        new_version = doc.version_bump(
            page_count=old_version.page_count - 1
        )

        remove_pdf_pages(
            old_version=old_version,
            new_version=new_version,
            pages_to_delete=pages_to_delete
        )

        page_map = get_assigns_after_delete(
            total_pages=old_version.page_count,
            deleted_pages=[item.number for item in pages_to_delete]
        )

        reuse_ocr_data(
            old_version=old_version,
            new_version=new_version,
            page_map=page_map
        )

        reuse_text_field(
            old_version=old_version,
            new_version=new_version,
            page_map=page_map
        )


class PagesView(RequireAuthMixin, GenericAPIView):
    serializer_class = PageDeleteSerializer

    @extend_schema(operation_id="Multiple pages delete")
    def delete(self, request):
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            # delete nodes with specified IDs
            self.delete_pages(page_ids=serializer.data['pages'])
            return Response(
                data=serializer.data,
                status=status.HTTP_204_NO_CONTENT
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    def delete_pages(self, page_ids):
        pages_to_delete = Page.objects.filter(pk__in=page_ids)

        first_page = pages_to_delete.first()

        for page in pages_to_delete:
            if page.is_archived:
                raise APIBadRequest(
                    detail='Deleting archived page is not allowed'
                )

        old_version = first_page.document_version

        count = old_version.pages.count()
        if count <= pages_to_delete.count():
            raise APIBadRequest(
                detail='Document version must have at least one page'
            )

        doc = old_version.document
        new_version = doc.version_bump(
            page_count=old_version.page_count - pages_to_delete.count()
        )

        remove_pdf_pages(
            old_version=old_version,
            new_version=new_version,
            pages_to_delete=pages_to_delete
        )

        page_map = get_assigns_after_delete(
            total_pages=old_version.page_count,
            deleted_pages=[item.number for item in pages_to_delete]
        )

        reuse_ocr_data(
            old_version=old_version,
            new_version=new_version,
            page_map=page_map
        )

        reuse_text_field(
            old_version=old_version,
            new_version=new_version,
            page_map=page_map
        )


class PagesReorderView(RequireAuthMixin, GenericAPIView):
    parser_classes = [JSONParser]
    renderer_classes = (JSONRenderer,)
    serializer_class = PagesReorderSerializer

    @extend_schema(operation_id="Reorder")
    def post(self, request):
        """
        Reorders pages within document.
        """
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            # delete nodes with specified IDs
            self.reorder_pages(pages_data=serializer.data['pages'])
            return Response(
                data=serializer.data,
                status=status.HTTP_204_NO_CONTENT
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def reorder_pages(self, pages_data):
        pages = Page.objects.filter(
            pk__in=[item['id'] for item in pages_data]
        )
        old_version = pages.first().document_version

        doc = old_version.document
        new_version = doc.version_bump(
            page_count=old_version.page_count
        )

        page_count = old_version.pages.count()

        reorder_pdf_pages(
            old_version=old_version,
            new_version=new_version,
            pages_data=pages_data,
            page_count=page_count
        )

        reordered_list = get_reordered_list(
            pages_data=pages_data,
            page_count=page_count
        )

        page_map = list(zip(range(1, page_count + 1), reordered_list))

        reuse_ocr_data(
            old_version=old_version,
            new_version=new_version,
            page_map=page_map
        )

        reuse_text_field(
            old_version=old_version,
            new_version=new_version,
            page_map=page_map
        )


class PagesRotateView(RequireAuthMixin, GenericAPIView):
    parser_classes = [JSONParser]
    renderer_classes = (JSONRenderer,)
    serializer_class = PagesRotateSerializer

    @extend_schema(operation_id="Rotate")
    def post(self, request):
        """
        Rortates one or multiple pages with given angle.
        """
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            # delete nodes with specified IDs
            self.rotate_pages(pages_data=serializer.data['pages'])
            return Response(
                data=serializer.data,
                status=status.HTTP_204_NO_CONTENT
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def rotate_pages(self, pages_data):
        pages = Page.objects.filter(
            pk__in=[item['id'] for item in pages_data]
        )
        old_version = pages.first().document_version

        doc = old_version.document
        new_version = doc.version_bump()

        rotate_pdf_pages(
            old_version=old_version,
            new_version=new_version,
            pages_data=annotate_page_data(pages, pages_data, 'angle')
        )

        # page mapping is 1 to 1 as rotation does not
        # add/remove any page
        page_map = [
            (page.number, page.number)
            for page in old_version.pages.all()
        ]

        reuse_text_field(
            old_version=old_version,
            new_version=new_version,
            page_map=page_map
        )


class PagesMoveToFolderView(RequireAuthMixin, GenericAPIView):
    serializer_class = PagesMoveToFolderSerializer
    renderer_classes = (JSONRenderer,)

    @extend_schema(operation_id="Move to folder")
    def post(self, request):
        """
        Moves/extracts one or multiple pages into target folder.

        This operation will create new one or multiple documents (depending
        on ``single_page`` parameter) and place then into target folder.
        ``single_page`` parameter is boolean value which controls whether all
        extracted pages will be placed inside one single document or each
        individual page will be placed into newly created single page document.
        """
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            self.move_to_folder(serializer.data)
            return Response(
                data=serializer.data,
                status=status.HTTP_204_NO_CONTENT
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def move_to_folder(self, data):
        pages = Page.objects.filter(pk__in=data['pages'])
        dst_folder = Folder.objects.get(
            pk=data['dst'],
            user=self.request.user
        )
        first_page = pages.first()

        src_old_version = first_page.document_version
        doc = src_old_version.document
        src_new_version = doc.version_bump(
            src_old_version.pages.count() - pages.count()
        )

        remove_pdf_pages(
            old_version=src_old_version,
            new_version=src_new_version,
            pages_to_delete=pages
        )

        page_map = get_assigns_after_delete(
            total_pages=src_old_version.page_count,
            deleted_pages=[item.number for item in pages]
        )

        reuse_ocr_data(
            old_version=src_old_version,
            new_version=src_new_version,
            page_map=page_map
        )
        reuse_text_field(
            old_version=src_old_version,
            new_version=src_new_version,
            page_map=page_map
        )

        if data['single_page']:
            # insert all pages in one single document
            self.move_to_folder_single_paged(
                pages=pages,
                dst_folder=dst_folder
            )
        else:
            # there will be one document for each page
            self.move_to_folder_multi_paged(
                pages=pages,
                first_page=first_page,
                dst_folder=dst_folder
            )

    def move_to_folder_multi_paged(self, pages, first_page, dst_folder):
        new_doc = Document.objects.create_document(
            title='noname.pdf',
            lang=first_page.lang,
            user_id=dst_folder.user_id,
            parent=dst_folder,
            ocr_status=OCR_STATUS_SUCCEEDED
        )
        # create new document version which
        # will contain mentioned pages
        dst_version = new_doc.version_bump_from_pages(pages=pages)
        page_map = zip(
            range(1, pages.count() + 1),
            [page.number for page in pages.order_by('number')]
        )
        for src_page, dst_page in zip(
            pages.order_by('number'),
            dst_version.pages.order_by('number'),
        ):
            get_storage_instance().copy_page(
                src=src_page.page_path,
                dst=dst_page.page_path
            )
        reuse_text_field(
            old_version=first_page.document_version,
            new_version=dst_version,
            page_map=page_map
        )

    def move_to_folder_single_paged(self, pages, dst_folder):
        for page in pages:
            doc = Document.objects.create_document(
                title=f'noname-{page.pk}.pdf',
                lang=page.lang,
                user_id=dst_folder.user_id,
                parent=dst_folder,
                ocr_status=OCR_STATUS_SUCCEEDED
            )
            # create new document version
            # with one page
            doc_version = doc.version_bump_from_pages(pages=[page])
            get_storage_instance().copy_page(
                src=page.page_path,
                dst=doc_version.pages.first().page_path
            )
            reuse_text_field(
                old_version=page.document_version,
                new_version=doc_version,
                page_map=[(1, page.number)]
            )


class PagesMoveToDocumentView(RequireAuthMixin, GenericAPIView):
    serializer_class = PagesMoveToDocumentSerializer
    renderer_classes = (JSONRenderer,)

    @extend_schema(operation_id="Move to document")
    def post(self, request):
        """
        Moves one or multiple pages from source document to target document.

        Both source and target documents' version will be incremented
        by one.
        """
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            self.move_to_document(serializer.data)
            return Response(
                data=serializer.data,
                status=status.HTTP_204_NO_CONTENT
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def move_to_document(self, data):
        pages = Page.objects.filter(
            pk__in=data['pages']
        )
        dst_document = Document.objects.get(
            pk=data['dst'],
            user=self.request.user
        )
        src_old_version = pages.first().document_version
        dst_old_version = dst_document.versions.last()

        doc = src_old_version.document
        src_new_version = doc.version_bump(
            page_count=src_old_version.pages.count() - pages.count()
        )
        dst_new_version = dst_document.version_bump(
            page_count=dst_old_version.pages.count() + pages.count()
        )

        remove_pdf_pages(
            old_version=src_old_version,
            new_version=src_new_version,
            pages_to_delete=pages
        )

        page_map = get_assigns_after_delete(
            total_pages=src_old_version.page_count,
            deleted_pages=[item.number for item in pages]
        )

        reuse_ocr_data(
            old_version=src_old_version,
            new_version=src_new_version,
            page_map=page_map
        )

        reuse_text_field(
            old_version=src_old_version,
            new_version=src_new_version,
            page_map=page_map
        )

        insert_pdf_pages(
            src_old_version=src_old_version,
            dst_old_version=dst_old_version,
            dst_new_version=dst_new_version,
            page_numbers=[p.number for p in pages.order_by('number')],
            position=data['position']
        )

        dst_page_map = [
            (p.number, p.number + data['position'])
            for p in pages.order_by('number')
        ]
        copy_pages_data(
            old_version=dst_old_version,
            new_version=dst_new_version,
            pages_map=dst_page_map
        )

        reuse_text_field_multi(
            src_old_version=src_old_version,
            dst_old_version=dst_old_version,
            dst_new_version=dst_new_version,
            position=data['position'],
            pages=pages
        )
