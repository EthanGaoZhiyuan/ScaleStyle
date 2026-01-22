# Configuration Guide

ScaleStyle inference service uses centralized configuration management via `src/config.py` and `src/ray_serve/config.yaml`.

## Configuration Structure

All configurations are organized into logical groups:

### 1. Redis Configuration (`RedisConfig`)
```python
REDIS_HOST = "localhost"           # Redis server hostname
REDIS_PORT = 6379                  # Redis server port
POPULARITY_KEY = "global:popular"  # Redis key for popular items list
```

### 2. Milvus Configuration (`MilvusConfig`)
```python
MILVUS_HOST = "localhost"                  # Milvus server hostname
MILVUS_PORT = "19530"                      # Milvus server port
MILVUS_COLLECTION = "scale_style_bge_v2"   # Collection name for vector search
```

### 3. Embedding Configuration (`EmbeddingConfig`)
```python
EMBEDDING_MODEL = "BAAI/bge-large-en-v1.5"  # HuggingFace model name
EMBEDDING_MAX_LEN = 512                      # Maximum token length
EMBEDDING_NUM_CPUS = 2                       # CPU cores for embedding service
EMBEDDING_NUM_GPUS = 0                       # GPU count (0 for CPU-only)
EMBEDDING_QUERY_PREFIX = "Represent..."     # Query prefix for asymmetric embedding
EMBEDDING_TIMEOUT_MS = 1200                  # Embedding timeout in milliseconds
```

### 4. Retrieval Configuration (`RetrievalConfig`)
```python
RECALL_K = 100                    # Number of candidates to recall from vector search
RETRIEVAL_TIMEOUT_MS = 800        # Milvus search timeout in milliseconds
```

### 5. Reranker Configuration (`RerankerConfig`)
```python
RERANKER_ENABLED = True                     # Enable/disable reranking
RERANKER_MODEL = "BAAI/bge-reranker-base"  # CrossEncoder model name
RERANKER_DEVICE = "cpu"                     # Device: cpu, cuda, or mps
RERANKER_BATCH_SIZE = 16                    # Batch size for inference
RERANKER_MAX_DOCS = 100                     # Maximum documents to rerank
RERANKER_MODE = "cross-encoder"             # Mode: cross-encoder or stub
RERANKER_TIMEOUT_MS = 1200                  # Reranker timeout in milliseconds
RERANKER_WARMUP = True                      # Run warmup on initialization
```

### 6. A/B Testing Configuration (`ABTestConfig`)
```python
BASE_FLOW_MODE = "vector"  # Base flow mode: "vector" or "popularity"
```

## Usage

### In Python Code

```python
from src.config import InferenceConfig

# Access Redis config
host = InferenceConfig.redis.HOST
port = InferenceConfig.redis.PORT

# Access Embedding config
model_name = InferenceConfig.embedding.MODEL
max_len = InferenceConfig.embedding.MAX_LENGTH

# Access Reranker config
if InferenceConfig.reranker.ENABLED:
    model = InferenceConfig.reranker.MODEL
```

### Environment Variable Override

All configurations can be overridden via environment variables:

```bash
# Override Redis connection
export REDIS_HOST="redis.production.com"
export REDIS_PORT="6380"

# Override embedding model
export EMBEDDING_MODEL="BAAI/bge-small-en-v1.5"
export EMBEDDING_NUM_GPUS="1"

# Disable reranker
export RERANKER_ENABLED="0"

# Run service
serve run src.ray_serve.main:app
```

### Ray Serve config.yaml

For production deployments, configure via `src/ray_serve/config.yaml`:

```yaml
applications:
  - name: scale_style_app
    runtime_env:
      env_vars:
        REDIS_HOST: "redis.prod.com"
        REDIS_PORT: "6379"
        EMBEDDING_MODEL: "BAAI/bge-large-en-v1.5"
        RERANKER_ENABLED: "1"
        # ... other configs
```

## Configuration Priorities

1. **Environment variables** (highest priority)
2. **config.yaml** (for Ray Serve deployments)
3. **config.py defaults** (lowest priority)

## Migration from Old Code

Previously, configurations were scattered across deployment files using `os.getenv()`:

```python
# ❌ Old way (hardcoded defaults)
host = os.getenv("REDIS_HOST", "localhost")
port = int(os.getenv("REDIS_PORT", "6379"))

# ✅ New way (centralized config)
from src.config import RedisConfig
host = RedisConfig.HOST
port = RedisConfig.PORT
```

Benefits:
- ✅ Single source of truth
- ✅ Type safety (automatic int/float/bool conversion)
- ✅ Easy to discover all available configurations
- ✅ Consistent defaults across all services
- ✅ Better IDE autocomplete support

## Testing Configuration

For testing, you can temporarily override configs:

```python
import os
os.environ["REDIS_HOST"] = "localhost"
os.environ["RERANKER_ENABLED"] = "0"

# Then import config
from src.config import InferenceConfig
# Config will use overridden values
```

Or use monkeypatch in pytest:

```python
def test_with_custom_config(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "test-redis")
    from src.config import RedisConfig
    assert RedisConfig.HOST == "test-redis"
```

## Common Configuration Scenarios

### Development (Local)
```bash
# Use local services
REDIS_HOST=localhost
MILVUS_HOST=localhost
EMBEDDING_NUM_GPUS=0
RERANKER_DEVICE=cpu
```

### Staging
```bash
# Use staging services
REDIS_HOST=redis.staging.scalestyle.com
MILVUS_HOST=milvus.staging.scalestyle.com
EMBEDDING_NUM_GPUS=0
RERANKER_ENABLED=1
```

### Production
```bash
# Use production services with GPU
REDIS_HOST=redis.prod.scalestyle.com
MILVUS_HOST=milvus.prod.scalestyle.com
EMBEDDING_NUM_GPUS=1
EMBEDDING_NUM_CPUS=4
RERANKER_DEVICE=cuda
RERANKER_ENABLED=1
```

### Debug/Testing
```bash
# Disable expensive operations
RERANKER_ENABLED=0
EMBEDDING_MODEL=stub
BASE_FLOW_MODE=popularity
```
