"""
Configuration for Feature Updater Service

Week 2: Real-time behavior loop
"""

import os

# Kafka Configuration
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "scalestyle.clicks")
KAFKA_GROUP_ID = os.getenv("KAFKA_GROUP_ID", "event-consumer")
KAFKA_AUTO_OFFSET_RESET = os.getenv("KAFKA_AUTO_OFFSET_RESET", "latest")

# Redis Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

# Feature Configuration
RECENT_CLICKS_MAX = int(
    os.getenv("RECENT_CLICKS_MAX", "100")
)  # Max recent clicks to keep per user
AFFINITY_DECAY_DAYS = int(
    os.getenv("AFFINITY_DECAY_DAYS", "7")
)  # Days for category affinity decay
SESSION_EXPIRE_SECONDS = int(os.getenv("SESSION_EXPIRE_SECONDS", "3600"))  # 1 hour
RECENT_ITEM_CLICKS_EXPIRE = int(
    os.getenv("RECENT_ITEM_CLICKS_EXPIRE", "86400")
)  # 24 hours

# Reliability Configuration
DEDUPE_WINDOW_SECONDS = int(
    os.getenv("DEDUPE_WINDOW_SECONDS", "604800")
)  # 7 days (matches Kafka retention)

# Processing Configuration
LOG_INTERVAL = int(os.getenv("LOG_INTERVAL", "100"))  # Log stats every N events
# Note: BATCH_SIZE removed - Lua script handles atomicity, no pipeline batching needed

# Observability Configuration
METRICS_PORT = int(os.getenv("METRICS_PORT", "8000"))  # HTTP metrics endpoint port
