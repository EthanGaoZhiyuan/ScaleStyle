"""
Configuration management for inference service.

This module centralizes all configuration parameters for the recommendation service,
including database connections, model settings, and operational parameters.
All values can be overridden via environment variables.
"""

import os


class RedisConfig:
    """Redis connection configuration."""

    HOST = os.getenv("REDIS_HOST", "localhost")
    PORT = int(os.getenv("REDIS_PORT", "6379"))
    POPULARITY_KEY = os.getenv("POPULARITY_KEY", "global:popular")


class MilvusConfig:
    """Milvus vector database configuration."""

    HOST = os.getenv("MILVUS_HOST", "localhost")
    PORT = os.getenv("MILVUS_PORT", "19530")
    COLLECTION = os.getenv("MILVUS_COLLECTION", "scale_style_bge_v2")


class EmbeddingConfig:
    """Embedding model configuration."""

    MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    MAX_LENGTH = int(os.getenv("EMBEDDING_MAX_LEN", "512"))
    NUM_CPUS = int(os.getenv("EMBEDDING_NUM_CPUS", "2"))
    NUM_GPUS = float(os.getenv("EMBEDDING_NUM_GPUS", "0"))
    QUERY_PREFIX = os.getenv(
        "EMBEDDING_QUERY_PREFIX",
        "Represent this sentence for searching relevant passages:",
    )
    TIMEOUT_MS = int(os.getenv("EMBEDDING_TIMEOUT_MS", "1200"))


class RetrievalConfig:
    """Retrieval and search configuration."""

    RECALL_K = int(os.getenv("RECALL_K", "100"))
    TIMEOUT_MS = int(os.getenv("RETRIEVAL_TIMEOUT_MS", "800"))


class RerankerConfig:
    """Reranker model configuration."""

    @staticmethod
    def _get_bool(name: str, default: bool = False) -> bool:
        """Parse boolean from environment variable."""
        val = os.getenv(name)
        if val is None:
            return default
        return val.strip().lower() in ("1", "true", "yes", "y", "on")

    ENABLED = _get_bool.__func__("RERANKER_ENABLED", True)
    MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base")
    DEVICE = os.getenv("RERANKER_DEVICE", "cpu")
    BATCH_SIZE = int(os.getenv("RERANKER_BATCH_SIZE", "16"))
    MAX_DOCS = int(os.getenv("RERANKER_MAX_DOCS", "100"))
    MODE = os.getenv("RERANKER_MODE", "cross-encoder")
    TIMEOUT_MS = int(os.getenv("RERANKER_TIMEOUT_MS", "1200"))
    WARMUP = _get_bool.__func__("RERANKER_WARMUP", True)


class ABTestConfig:
    """A/B testing configuration."""

    BASE_FLOW_MODE = os.getenv(
        "BASE_FLOW_MODE", "vector"
    ).lower()  # vector or popularity


class InferenceConfig:
    """Main configuration class aggregating all sub-configs."""

    redis = RedisConfig
    milvus = MilvusConfig
    embedding = EmbeddingConfig
    retrieval = RetrievalConfig
    reranker = RerankerConfig
    ab_test = ABTestConfig


# Legacy support for data-pipeline (if still needed)
class Config:
    DEFAULT_DATA_PATH = "../data-pipeline/data/processed/"
    DATA_PATH = os.getenv("DATA_PATH", DEFAULT_DATA_PATH)
    PORT = 50051
