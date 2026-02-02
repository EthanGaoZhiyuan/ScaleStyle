"""
Milvus client utilities with connection retry logic.

Provides factory functions for creating Milvus client instances with
automatic retry on connection failures, suitable for distributed deployments
where Milvus may take time to start up.
"""

import logging
import os
import time
from pymilvus import MilvusClient as PyMilvusClient, connections

logger = logging.getLogger(__name__)


def create_milvus_client(
    uri: str = None,
    host: str = None,
    port: str = None,
    max_retries: int = 10,  # Increased to 10 retries (production-grade configuration)
    retry_delay: float = 3.0,  # Increased to 3s delay (with exponential backoff)
) -> PyMilvusClient:
    """
    Create a Milvus client with automatic retry logic and exponential backoff.

    Args:
        uri: Full URI for Milvus (e.g., "http://milvus:19530"). Takes precedence over host/port.
        host: Milvus host (e.g., "milvus"). Used if uri is not provided.
        port: Milvus port (e.g., "19530"). Used if uri is not provided.
        max_retries: Maximum number of connection attempts (default: 10 for production).
        retry_delay: Initial delay in seconds between retries (will use exponential backoff).

    Returns:
        Connected MilvusClient instance.

    Raises:
        ConnectionError: If connection fails after all retries.

    Example:
        >>> client = create_milvus_client(uri="http://milvus:19530")
        >>> client = create_milvus_client(host="milvus", port="19530")
    """
    # Determine URI - read from environment at runtime, not import time
    if uri is None:
        if host is None:
            host = os.getenv("MILVUS_HOST", "localhost")
        if port is None:
            port = os.getenv("MILVUS_PORT", "19530")
        uri = f"http://{host}:{port}"

    # Retry connection with exponential backoff
    last_error = None
    for attempt in range(max_retries):
        try:
            logger.info(
                f"🔌 Connecting to Milvus at {uri} (attempt {attempt + 1}/{max_retries})"
            )
            client = PyMilvusClient(uri=uri)

            # Verify connection is actually usable
            try:
                client.list_collections()
                logger.info(f"✅ Milvus connection successful to {uri}")
                return client
            except Exception as verify_error:
                logger.warning(
                    f"Connection established but verification failed: {verify_error}"
                )
                raise verify_error

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                # Exponential backoff: 2^attempt * retry_delay, max 30 seconds
                backoff_delay = min(retry_delay * (2**attempt), 30.0)
                logger.warning(
                    f"⚠️  Milvus connection attempt {attempt + 1}/{max_retries} failed: {e}. "
                    f"Retrying in {backoff_delay:.1f}s..."
                )
                time.sleep(backoff_delay)
            else:
                logger.error(
                    f"❌ Failed to connect to Milvus at {uri} after {max_retries} attempts. "
                    f"Last error: {type(e).__name__}: {e}"
                )

    raise ConnectionError(
        f"Could not connect to Milvus at {uri} after {max_retries} attempts. "
        f"Last error: {last_error}"
    )


# Legacy function for backward compatibility (uses low-level connections API)
def ensure_connection(
    alias: str = "default", max_retries: int = 3, retry_delay: float = 2.0
):
    """
    Ensure a connection exists using the low-level connections API.

    DEPRECATED: Use create_milvus_client() instead for new code.
    This function is kept for backward compatibility with existing scripts.

    Args:
        alias: Connection alias name.
        max_retries: Maximum number of connection attempts.
        retry_delay: Delay in seconds between retries.

    Raises:
        ConnectionError: If connection fails after all retries.
    """
    # Read from environment at runtime to avoid serialization issues
    host = os.getenv("MILVUS_HOST", "localhost")
    port = os.getenv("MILVUS_PORT", "19530")

    # Check if connection already exists
    if connections.has_connection(alias):
        logger.info(f"Milvus connection '{alias}' already exists")
        return

    # Retry connection
    last_error = None
    for attempt in range(max_retries):
        try:
            logger.info(
                f"Connecting to Milvus at {host}:{port} (attempt {attempt + 1}/{max_retries})"
            )
            connections.connect(alias, host=host, port=port)
            logger.info(f"✅ Milvus connection '{alias}' successful")
            return
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                logger.warning(
                    f"Connection attempt {attempt + 1} failed: {e}. Retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
            else:
                logger.error(f"❌ Failed to connect after {max_retries} attempts")

    raise ConnectionError(
        f"Could not connect to Milvus at {host}:{port} after {max_retries} attempts. "
        f"Last error: {last_error}"
    )
