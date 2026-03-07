# Event Consumer Service

**Week 2**: Real-time behavior loop - Kafka consumer that processes events and updates Redis features

## Purpose

Consumes user interaction events from Kafka and updates online features in Redis for real-time personalization.

## Architecture

```
Kafka (scalestyle.clicks) 
  → Event Consumer (this service)
  → Redis (online features)
  → Inference Service (rerank with updated features)
```

## Features Updated

### User Features
- `user:{uid}:recent_clicks` - List of recently clicked item IDs (last 100)
- `user:{uid}:category_affinity:{cat}` - Category affinity scores with time decay
- `user:{uid}:last_activity` - Last activity timestamp

### Item Features  
- `item:{item_id}:clicks` - Total click count (counter)
- `item:{item_id}:recent_clicks` - Recent click count (last 24h, expires)
- `global:popular` - Global popularity sorted set (item_id → score)

### Session Features
- `session:{sid}:clicks` - Clicks in current session (expires after 1 hour)

## Event Schema

See `docs/contracts/events.md` for full schema.

```json
{
  "event_type": "click",
  "user_id": "user_12345",
  "item_id": "108775051",
  "timestamp": "2026-02-23T10:30:45.123Z",
  "session_id": "sess_abc123",
  "source": "search",
  "query": "red dress",
  "position": 2
}
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka broker addresses |
| `KAFKA_TOPIC` | `scalestyle.clicks` | Topic to consume |
| `KAFKA_GROUP_ID` | `event-consumer` | Consumer group ID |
| `REDIS_HOST` | `localhost` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `RECENT_CLICKS_MAX` | `100` | Max recent clicks per user |
| `AFFINITY_DECAY_DAYS` | `7` | Days for affinity decay |

## Running

### Local Development
```bash
python -m pip install -r requirements.txt
python src/consumer.py
```

### Docker
```bash
docker-compose up event-consumer
```

## Monitoring

- Logs: Outputs event processing stats every 100 events
- Metrics: Redis key counts, processing latency
- Health: Check Redis connectivity on startup
