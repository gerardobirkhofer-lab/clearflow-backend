import redis.asyncio as redis
from functools import lru_cache

from .config import get_settings


@lru_cache
def get_redis():
    settings = get_settings()
    return redis.from_url(settings.REDIS_URL, decode_responses=True)
