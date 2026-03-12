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
                        # socket_connect_timeout=0.02 (20 ms): TCP handshake budget.  A new
                        #   connection that takes longer than this is already outside the p99
                        #   window; fail fast and let the caller degrade gracefully.
                        # socket_timeout=0.01 (10 ms): per-command read/write budget.
                        #   Matches the p99 < 10 ms personalization-read SLO.  Any Redis op
                        #   that exceeds this raises redis.TimeoutError immediately — the
                        #   caller (FeatureReader) catches it and returns an empty fallback.
                        #
                        # NO automatic retry (retry_on_timeout=False, no retry_on_error)
                        # ---------------------------------------------------------------
                        # Retrying on the hot path would silently double (or triple) the
                        # tail latency: a 10 ms timeout that retries twice becomes 30 ms+
                        # before the business layer even sees the failure.  Degradation
                        # (empty features → popularity fallback) is cheaper and predictable.
                        # Retry logic belongs in the event-consumer's offline write path,
                        # not here.
                        pool = redis.ConnectionPool(
                            host=RedisConfig.HOST,
                            port=RedisConfig.PORT,
                            decode_responses=True,
                            socket_connect_timeout=0.02,  # 20 ms — fail-fast on new connections
                            socket_timeout=0.01,  # 10 ms — matches p99 personalization SLO
                            max_connections=128,  # support concurrent Ray Serve replicas
                            health_check_interval=30,  # keep idle connections alive
                            retry_on_timeout=False,  # NO implicit retry — see note above
                            ssl=RedisConfig.TLS,
                            ssl_cert_reqs="required" if RedisConfig.TLS else None,
                        )

                        cls._client = redis.Redis(connection_pool=pool)
                        logger.info("✅ Redis connection pool initialized successfully")

                    except Exception as e:
                        logger.error(
                            "❌ Failed to connect to Redis: %s", e, exc_info=True
                        )
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
