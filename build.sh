#!/bin/bash
# Week 2: Build All Services
# Downloads dependencies and builds Docker images

set -e

echo "🔨 Building ScaleStyle services..."
echo ""

# Build Gateway (download Kafka dependencies)
echo "📦 Building Gateway Service..."
cd gateway-service
./gradlew clean build -x test
cd ..

echo ""
echo "📦 Building Inference Service..."
# Inference service builds in Docker

echo ""
echo "📦 Building Event Consumer..."
# Event consumer builds in Docker

echo ""
echo "🐳 Building Docker images..."
docker-compose build

echo ""
echo "✅ Build complete!"
echo ""
echo "🚀 To start services:"
echo "   ./start.sh"
echo ""
echo "🧪 To test behavior loop:"
echo "   cd tests && python test_behavior_loop.py"
