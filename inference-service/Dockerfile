# Use a lightweight Python image as the base
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install PYthon dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire service code into the container
COPY . .

# Set environment variables
# PYTHONUNBUFFERED ensures that the Python output is sent straight to terminal (e.g., your container log) without being buffered
ENV PYTHONUNBUFFERED=1

# PYTHONPATH adds the proto directory to the Python path
ENV PYTHONPATH="/app/src/proto"

# Expose the gRPC port
EXPOSE 50051

# Command to run the inference service
CMD ["python", "server.py"]
