"""
Redis Client - Production-grade connection pool and configuration

Provides structured logging, timeouts, connection pooling, and health checks
for Ray Serve replicas.
"""

import logging
import threading
import redis
from src.config import RedisConfig

logger = logging.getLogger(__name__)


class RedisClient:
    """
    Singleton Redis client with production-grade configuration.

    Features:
    - Connection pooling for concurrent Ray Serve replicas
    - Socket and connect timeouts to prevent hang
    - Health check interval for connection validation
    - Structured logging instead of print statements
    - Proper error handling and retry configuration
    """

    _instance = None
    _client = None
    _lock = threading.Lock()  # Protect concurrent initialization

    @classmethod
    def get_client(cls):
        """
        Get or create Redis client with connection pool.
        Thread-safe singleton with double-checked locking.

        Returns:
            redis.Redis: Configured Redis client instance
        """
        if cls._client is None:
            with cls._lock:
                # Double-check after acquiring lock
                if cls._client is None:
                    logger.info(
                        "Initializing Redis connection pool: host=%s, port=%s",
                        RedisConfig.HOST,
                        RedisConfig.PORT,
                    )

                    try:
                        # Create connection pool for the Ray Serve hot path.
                        #
                        # Timeout rationale
                        # -----------------
                        # socket_connect_timeout (150 ms default): TCP handshake budget.
                        #   10 ms was correct for localhost but too tight for Docker bridge
                        #   (~1–5 ms round-trip) or ElastiCache (~0.5–3 ms + spikes).
                        #   150 ms gives ample room for transient network jitter without
                        #   hanging the event loop.
                        # socket_timeout (150 ms default): per-command read/write deadline.
                        #   The application-level guard (PERSONALIZATION_TIMEOUT_MS=50 ms
                        #   asyncio.wait_for) fires first on the hot path; this is the
                        #   socket-level last resort for commands that don't go through
                        #   wait_for (e.g. startup ping, enrich pipeline).
                        # Both values are configurable via REDIS_CONNECT_TIMEOUT_MS and
                        # REDIS_SOCKET_TIMEOUT_MS environment variables (see RedisConfig).
                        #
                        # NO automatic retry (retry_on_timeout=False, no retry_on_error)
                        # ---------------------------------------------------------------
                        # Retrying on the hot path would silently multiply tail latency.
                        # Degradation (empty features → popularity fallback) is cheaper
                        # and predictable.  Retry logic belongs in the event-consumer.
                        pool_kwargs = dict(
                            host=RedisConfig.HOST,
                            port=RedisConfig.PORT,
                            decode_responses=True,
                            socket_connect_timeout=RedisConfig.SOCKET_CONNECT_TIMEOUT_SEC,
                            socket_timeout=RedisConfig.SOCKET_TIMEOUT_SEC,
                            max_connections=128,  # support concurrent Ray Serve replicas
                            health_check_interval=30,  # keep idle connections alive
                            retry_on_timeout=False,  # NO implicit retry — see note above
                        )
                        # redis-py 7.x dropped ssl=False as a valid kwarg; only
                        # pass TLS params when TLS is actually required.
                        if RedisConfig.TLS:
                            pool_kwargs["ssl"] = True
                            pool_kwargs["ssl_cert_reqs"] = "required"
                        pool = redis.ConnectionPool(**pool_kwargs)

                        cls._client = redis.Redis(connection_pool=pool)
                        logger.info("Redis connection pool initialized successfully")

                    except Exception as e:
                        logger.error("Failed to connect to Redis: %s", e, exc_info=True)
                        raise

        return cls._client


def validate_startup_connection() -> redis.Redis:
    """
    Validate Redis connectivity during process startup.

    Returns:
        redis.Redis: Shared Redis client after a successful ping.

    Raises:
        Exception: Re-raises the underlying Redis connectivity error.
    """
    client = RedisClient.get_client()

    try:
        client.ping()
        logger.info(
            "Redis startup validation succeeded: host=%s, port=%s",
            RedisConfig.HOST,
            RedisConfig.PORT,
        )
        return client
    except Exception:
        logger.error(
            "Redis startup validation failed: host=%s, port=%s",
            RedisConfig.HOST,
            RedisConfig.PORT,
            exc_info=True,
        )
        raise
