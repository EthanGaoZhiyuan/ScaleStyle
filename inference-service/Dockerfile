# Use official Python slim image as base
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH="/app"

# HuggingFace cache (container-level defaults; compose/k8s should mount volume)
ENV HF_HOME=/cache/huggingface
ENV TRANSFORMERS_CACHE=/cache/huggingface/hub
ENV HF_HUB_DISABLE_PROGRESS_BARS=1

# Expose port for Ray Serve
EXPOSE 8000

# Start Ray Serve with the specified configuration
CMD ["serve", "run", "src/ray_serve/config.yaml"]
