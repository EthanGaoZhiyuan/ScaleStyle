"""
Configuration management for inference service.

Centralizes all configuration parameters for the recommendation service,
including database connections, model settings, and operational parameters.
All values can be overridden via environment variables.
"""

import math
import os


def _get_bool_env(name: str, default: bool = False) -> bool:
    """Parse boolean from environment variable."""
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


class RedisConfig:
    """Redis connection configuration."""

    HOST = os.getenv("REDIS_HOST", "localhost")
    PORT = int(os.getenv("REDIS_PORT", "6379"))
    POPULARITY_KEY = os.getenv("POPULARITY_KEY", "global:popular")
    POPULARITY_BUCKET_PREFIX = os.getenv("POPULARITY_BUCKET_PREFIX", "popularity:bucket")
    POPULARITY_MATERIALIZED_PREFIX = os.getenv("POPULARITY_MATERIALIZED_PREFIX", "popularity:materialized")
    POPULARITY_PRIMARY_WINDOW = os.getenv("POPULARITY_PRIMARY_WINDOW", "24h")
    POPULARITY_SECONDARY_WINDOW = os.getenv("POPULARITY_SECONDARY_WINDOW", "7d")
    POPULARITY_1H_BUCKET_SECONDS = int(os.getenv("POPULARITY_1H_BUCKET_SECONDS", "300"))
    POPULARITY_24H_BUCKET_SECONDS = int(os.getenv("POPULARITY_24H_BUCKET_SECONDS", "3600"))
    POPULARITY_7D_BUCKET_SECONDS = int(os.getenv("POPULARITY_7D_BUCKET_SECONDS", "86400"))
    POPULARITY_1H_BUCKET_COUNT = int(os.getenv("POPULARITY_1H_BUCKET_COUNT", "12"))
    POPULARITY_24H_BUCKET_COUNT = int(os.getenv("POPULARITY_24H_BUCKET_COUNT", "24"))
    POPULARITY_7D_BUCKET_COUNT = int(os.getenv("POPULARITY_7D_BUCKET_COUNT", "7"))
    POPULARITY_1H_MATERIALIZED_TTL_SECONDS = int(os.getenv("POPULARITY_1H_MATERIALIZED_TTL_SECONDS", "60"))
    POPULARITY_24H_MATERIALIZED_TTL_SECONDS = int(os.getenv("POPULARITY_24H_MATERIALIZED_TTL_SECONDS", "300"))
    POPULARITY_7D_MATERIALIZED_TTL_SECONDS = int(os.getenv("POPULARITY_7D_MATERIALIZED_TTL_SECONDS", "900"))
    CATEGORY_AFFINITY_TIMESTAMP_SUFFIX = os.getenv(
        "CATEGORY_AFFINITY_TIMESTAMP_SUFFIX",
        ":last_ts",
    )
    CATEGORY_AFFINITY_HALF_LIFE_DAYS = float(
        os.getenv(
            "CATEGORY_AFFINITY_HALF_LIFE_DAYS",
            os.getenv("AFFINITY_DECAY_DAYS", "7"),
        )
    )
    CATEGORY_AFFINITY_DECAY_LAMBDA = math.log(2.0) / (CATEGORY_AFFINITY_HALF_LIFE_DAYS * 86400.0)
    # Enable TLS in production (EKS) against ElastiCache transit encryption.
    TLS = os.getenv("REDIS_TLS", "false").lower() in ("1", "true", "yes")


class MilvusConfig:
    """Milvus vector database configuration."""

    HOST = os.getenv("MILVUS_HOST", "localhost")
    PORT = os.getenv("MILVUS_PORT", "19530")
    COLLECTION = os.getenv("MILVUS_COLLECTION", "scale_style_bge_v2")


class EmbeddingConfig:
    """Embedding model configuration."""

    MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    MAX_LENGTH = int(os.getenv("EMBEDDING_MAX_LEN", "512"))
    # Keep the default Ray CPU reservation aligned with the 2 vCPU EKS inference pod.
    NUM_CPUS = float(os.getenv("EMBEDDING_NUM_CPUS", "0.5"))
    NUM_GPUS = float(os.getenv("EMBEDDING_NUM_GPUS", "0"))
    QUERY_PREFIX = os.getenv(
        "EMBEDDING_QUERY_PREFIX",
        "Represent this sentence for searching relevant passages:",
    )
    # Production-viable timeout for CPU-based BGE-large inference.
    # A cold inference can take 200-500ms on CPU; 500ms allows headroom.
    # GPU nodes should override to ~50-100ms via environment variable.
    TIMEOUT_MS = int(os.getenv("EMBEDDING_TIMEOUT_MS", "500"))


class RetrievalConfig:
    """Retrieval and search configuration."""

    RECALL_K = int(os.getenv("RECALL_K", "100"))
    # Production-viable timeout for Milvus ANN query over 100K+ vectors.
    # Warm queries typically take 100-200ms; 300ms allows headroom for cold starts.
    # GPU-accelerated or smaller collections can override to ~100ms.
    TIMEOUT_MS = int(os.getenv("RETRIEVAL_TIMEOUT_MS", "300"))


class RerankerConfig:
    """Reranker model configuration."""

    ENABLED = _get_bool_env("RERANKER_ENABLED", True)
    MODEL = os.getenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    DEVICE = os.getenv("RERANKER_DEVICE", "cpu")
    BATCH_SIZE = int(os.getenv("RERANKER_BATCH_SIZE", "16"))
    MAX_DOCS = int(os.getenv("RERANKER_MAX_DOCS", "50"))
    MODE = os.getenv("RERANKER_MODE", "cross-encoder")
    # Production-viable timeout for CPU-based cross-encoder reranking.
    # Batch inference over 50 docs typically takes 150-200ms; 250ms allows headroom.
    # GPU nodes should override to ~50-100ms via environment variable.
    TIMEOUT_MS = int(os.getenv("RERANKER_TIMEOUT_MS", "250"))
    WARMUP = _get_bool_env("RERANKER_WARMUP", True)


class ABTestConfig:
    """A/B testing configuration."""

    BASE_FLOW_MODE = os.getenv("BASE_FLOW_MODE", "vector").lower()


class GenerationConfig:
    """Generation model configuration."""

    ENABLED = _get_bool_env("GENERATION_ENABLED", False)
    MODE = os.getenv("GENERATION_MODE", "template").lower()
    TIMEOUT_MS = int(os.getenv("GENERATION_TIMEOUT_MS", "10"))
    FLOW = os.getenv("GENERATION_FLOW", "smart")
    MODEL = os.getenv(
        "GENERATION_MODEL",
        os.getenv("GEN_MODEL_NAME", "Qwen/Qwen2-1.5B-Instruct"),
    )
    REVISION = os.getenv(
        "GENERATION_REVISION",
        os.getenv("GEN_MODEL_REVISION", "ba1cf1846d7df0a0591d6c00649f57e798519da8"),
    )
    DEVICE = os.getenv("GENERATION_DEVICE", "auto")
    MAX_NEW_TOKENS = int(os.getenv("GENERATION_MAX_NEW_TOKENS", "48"))
    TEMPERATURE = float(os.getenv("GENERATION_TEMPERATURE", "0.2"))
    TOP_P = float(os.getenv("GENERATION_TOP_P", "0.9"))
    DO_SAMPLE = _get_bool_env("GENERATION_DO_SAMPLE", False)
    WARMUP = _get_bool_env("GENERATION_WARMUP", True)


class PersonalizationConfig:
    """Personalization behavior boost configuration."""

    ENABLED = _get_bool_env("PERSONALIZATION_ENABLED", True)
    EXACT_CLICK_BOOST = float(os.getenv("EXACT_CLICK_BOOST", "1.5"))
    CATEGORY_AFFINITY_BOOST = float(os.getenv("CATEGORY_AFFINITY_BOOST", "1.2"))
    POPULARITY_1H_BOOST = float(os.getenv("POPULARITY_1H_BOOST", "0.20"))
    POPULARITY_24H_BOOST = float(os.getenv("POPULARITY_24H_BOOST", "0.10"))
    POPULARITY_7D_BOOST = float(os.getenv("POPULARITY_7D_BOOST", "0.05"))
    MAX_RECENT_CLICKS_USED = int(os.getenv("MAX_RECENT_CLICKS_USED", "20"))
    DEBUG_MODE = _get_bool_env("PERSONALIZATION_DEBUG", False)


class InferenceConfig:
    """Main configuration class aggregating all sub-configs."""

    redis = RedisConfig
    milvus = MilvusConfig
    embedding = EmbeddingConfig
    retrieval = RetrievalConfig
    reranker = RerankerConfig
    ab_test = ABTestConfig
    generation = GenerationConfig
    personalization = PersonalizationConfig


class Config:
    """Legacy data pipeline configuration."""

    DEFAULT_DATA_PATH = "../data-pipeline/data/processed/"
    DATA_PATH = os.getenv("DATA_PATH", DEFAULT_DATA_PATH)
    PORT = 50051

