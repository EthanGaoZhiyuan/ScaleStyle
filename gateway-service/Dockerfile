# ==========================================
# Stage 1: Build Stage
# ==========================================
FROM gradle:8.5-jdk21 AS builder

WORKDIR /app

# 1. Copy dependency configuration first (better layer caching)
COPY build.gradle settings.gradle ./

# 2. Download dependencies (cached if build files unchanged)
# Use BuildKit cache mount for Gradle cache (speeds up CI builds)
RUN --mount=type=cache,target=/root/.gradle \
    gradle dependencies --no-daemon || true

# 3. Copy source code
COPY src ./src

# 4. Build the application JAR (skip tests for image build)
RUN --mount=type=cache,target=/root/.gradle \
    gradle bootJar -x test --no-daemon

# ==========================================
# Stage 2: Runtime Stage (Production-ready)
# ==========================================
FROM eclipse-temurin:21-jre-alpine

# OCI Labels for traceability (best practice for production)
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="ScaleStyle Gateway Service" \
      org.opencontainers.image.description="API Gateway for ScaleStyle recommendation system" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/EthanGaoZhiyuan/ScaleStyle" \
      org.opencontainers.image.vendor="ScaleStyle Team"

# Install curl for health checks (alpine needs explicit install)
RUN apk add --no-cache curl

WORKDIR /app

# 5. Copy JAR from builder
COPY --from=builder /app/build/libs/*.jar app.jar

# 6. Copy OpenTelemetry Java Agent (pre-downloaded for supply chain security)
# Version: v2.2.0
# SHA256: 7d12c02ef4f6c0063bc231d8ac3d1b7f8fe4861d1ee7a92b04fbdaa6fa3cf675
COPY --chmod=644 opentelemetry-javaagent.jar /app/opentelemetry-javaagent.jar

# 7. Create non-root user for security (production best practice)
RUN addgroup -S scalestyle && adduser -S scalestyle -G scalestyle && \
    chown -R scalestyle:scalestyle /app

USER scalestyle

# 8. Expose application port
EXPOSE 8080

# 9. Health check (K8s will use this endpoint)
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8080/actuator/health || exit 1

# 10. JVM tuning for container environment
ENV JAVA_OPTS="-XX:+UseContainerSupport -XX:MaxRAMPercentage=75.0 -XX:+ExitOnOutOfMemoryError"

# 11. Start application with OTel instrumentation
# Use 'exec' to ensure Java becomes PID1 for proper SIGTERM handling (K8s graceful shutdown)
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS \
    -javaagent:/app/opentelemetry-javaagent.jar \
    -Dotel.service.name=gateway-service \
    -Dotel.traces.exporter=otlp \
    -Dotel.exporter.otlp.protocol=grpc \
    -Dotel.metrics.exporter=none \
    -Dotel.logs.exporter=none \
    -jar app.jar"]