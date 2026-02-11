# ==========================================
# Data Init Image for Kubernetes Job
# Pre-bakes all Python dependencies for fast initialization
# ==========================================
FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy data initialization scripts
COPY src/ ./src/

# Copy processed data files (parquet with embeddings)
COPY data/processed/ ./data/processed/

# Create non-root user
RUN useradd -m -u 1000 dataloader && \
    chown -R dataloader:dataloader /app

USER dataloader

# Default: Run bootstrap script
CMD ["python", "-m", "src.bootstrap_data"]
