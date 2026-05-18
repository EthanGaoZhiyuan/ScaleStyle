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

# Install runtime dependencies: curl (healthcheck) + gosu (privilege drop in entrypoint)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code and entrypoint
COPY src ./src
COPY entrypoint.sh /entrypoint.sh

# Create non-root user; entrypoint chowns the HF cache volume at runtime
# (Docker named volumes are root-owned at creation; gosu drops to scalestyle)
RUN useradd -m -u 1000 scalestyle && \
    mkdir -p /cache/huggingface && \
    chown -R scalestyle:scalestyle /app /cache && \
    chmod +x /entrypoint.sh

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

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "src/server.py"]

