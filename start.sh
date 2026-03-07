#!/bin/bash
# Week 2: Quick Start Script
# Start all services and verify health

set -e

echo "🚀 Starting ScaleStyle with real-time behavior loop (Week 2)..."
echo ""

# Start services
echo "📦 Starting Docker Compose services..."
docker-compose up -d

echo ""
echo "⏳ Waiting for services to be healthy..."
sleep 5

# Check service health
echo ""
echo "🔍 Checking service health..."

# Check Gateway
if curl -sf http://localhost:8080/actuator/health > /dev/null 2>&1; then
  echo "✅ Gateway Service (8080) - HEALTHY"
else
  echo "❌ Gateway Service (8080) - NOT READY"
fi

# Check Inference
if curl -sf http://localhost:8000/healthz > /dev/null 2>&1; then
  echo "✅ Inference Service (8000) - HEALTHY"
else
  echo "❌ Inference Service (8000) - NOT READY"
fi

# Check Redis
if docker exec scalestyle-redis redis-cli ping > /dev/null 2>&1; then
  echo "✅ Redis (6379) - HEALTHY"
else
  echo "❌ Redis (6379) - NOT READY"
fi

# Check Kafka
if docker exec scalestyle-kafka kafka-topics --list --bootstrap-server localhost:9092 > /dev/null 2>&1; then
  echo "✅ Kafka (9092) - HEALTHY"
else
  echo "⚠️  Kafka (9092) - NOT READY (may need more time)"
fi

# Check Feature Updater
if docker ps | grep scalestyle-event-consumer | grep Up > /dev/null 2>&1; then
  echo "✅ Event Consumer - RUNNING"
else
  echo "❌ Event Consumer - NOT RUNNING"
fi

echo ""
echo "📋 Service URLs:"
echo "   Gateway API: http://localhost:8080/swagger-ui"
echo "   Jaeger UI: http://localhost:16686"
echo "   Grafana: http://localhost:3000 (admin/admin)"
echo "   Prometheus: http://localhost:9090"

echo ""
echo "🧪 To test behavior loop:"
echo "   cd tests && python test_behavior_loop.py"
echo "   OR"
echo "   cd tests && ./test_behavior_loop.sh"

echo ""
echo "📊 To view logs:"
echo "   docker-compose logs -f event-consumer"
echo "   docker-compose logs -f gateway-service"
echo "   docker-compose logs -f inference-service"

echo ""
echo "✅ Startup complete!"
