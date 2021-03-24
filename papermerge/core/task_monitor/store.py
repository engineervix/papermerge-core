from redis import Redis


class GenericStore(dict):
    """
    Generic store used in testing.
    """

    def expire(self, key):
        pass


class RedisStore:
    """
    Redis store used (by default) in development and production.
    """

    def __init__(self, url, timeout):
        # With decode_responses=True argument redis client will
        # automatically encode returned bytes to UTF-8 strings
        self.redis = Redis.from_url(url, decode_repsonses=True)
        # keys timeout in seconds
        self.timeout = timeout

    def __getitem__(self, key):
        return self.redis.hgetall(key)

    def __setitem__(self, key, value):
        self.redis.hmset(key, value)

    def expire(self, key):
        self.redis.expire(key, self.timeout)
