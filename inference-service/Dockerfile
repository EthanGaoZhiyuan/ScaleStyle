# ==========================================
# Stage 1: Builder Stage
# ==========================================
FROM python:3.10-slim AS builder

WORKDIR /build

# Install system dependencies for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (better layer caching)
COPY requirements.txt .

# Install Python dependencies to a specific directory
# Use BuildKit cache mount for pip cache (speeds up CI builds)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.txt

# ==========================================
# Stage 2: Runtime Stage (Production-ready)
# ==========================================
FROM python:3.10-slim

# OCI Labels for traceability (best practice for production)
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="ScaleStyle Inference Service" \
      org.opencontainers.image.description="AI-powered recommendation inference service with Ray Serve" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/EthanGaoZhiyuan/ScaleStyle" \
      org.opencontainers.image.vendor="ScaleStyle Team"

WORKDIR /app

# Install runtime dependencies only (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY src ./src
COPY server.py .

# Create non-root user for security
RUN useradd -m -u 1000 scalestyle && \
    mkdir -p /cache/huggingface && \
    chown -R scalestyle:scalestyle /app /cache

USER scalestyle

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH="/app"
ENV HF_HOME=/cache/huggingface
ENV TRANSFORMERS_CACHE=/cache/huggingface/hub
ENV HF_HUB_DISABLE_PROGRESS_BARS=1
ENV HF_HUB_DISABLE_TELEMETRY=1
ENV TOKENIZERS_PARALLELISM=false

# Expose Ray Serve port
EXPOSE 8000

# Health check for K8s readiness probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/readyz || exit 1

# Fix: Use config.yaml (GENERATION_ENABLED=1) to match ConfigMap settings
# Use RAY_SERVE_CONFIG env var to override if needed (e.g., for minimal testing)
ENV RAY_SERVE_CONFIG=src/ray_serve/config.yaml
CMD ["sh", "-c", "serve run $RAY_SERVE_CONFIG"]

