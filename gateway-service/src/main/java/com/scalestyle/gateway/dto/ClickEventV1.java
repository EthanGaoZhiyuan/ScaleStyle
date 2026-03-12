package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * Kafka event contract v1 for click tracking.
 *
 * <p>Timestamp fields:
 * <ul>
 *   <li>{@code timestamp} — ISO-8601 string, e.g. {@code "2026-03-08T10:15:30.123Z"}.
 *       Kept for human readability and backward compatibility with consumers that
 *       already parse it for the {@code user:{id}:last_activity} Redis write.</li>
 *   <li>{@code event_timestamp_ms} — Unix epoch milliseconds (Long).
 *       Preferred for numeric sorting, range queries, Flink/Spark watermarking,
 *       and analytics joins.  Absent (null) only on events produced before this
 *       field was introduced; treat null as "unknown" on the consumer side.</li>
 * </ul>
 * Both fields represent the same instant and are populated atomically at the
 * producer so they are always consistent with each other when present.
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class ClickEventV1 {

    @JsonProperty("event_type")
    private String eventType;

    @JsonProperty("event_id")
    private String eventId;

    @JsonProperty("user_id")
    private String userId;

    @JsonProperty("item_id")
    private String itemId;

    /**
     * ISO-8601 string timestamp (e.g., "2026-03-08T10:15:30.123Z").
     * Preserved for backward compatibility with existing consumers.
     */
    @JsonProperty("timestamp")
    private String timestamp;

    /**
     * Unix epoch milliseconds timestamp.
     * Preferred for numeric comparisons, range queries, and analytics pipelines.
     * Returns null only for events produced before this field was introduced.
     */
    @JsonProperty("event_timestamp_ms")
    private Long eventTimestampMs;

    @JsonProperty("session_id")
    private String sessionId;

    @JsonProperty("source")
    private String source;

    @JsonProperty("query")
    private String query;

    @JsonProperty("image_hash")
    private String imageHash;

    @JsonProperty("position")
    private Integer position;

    @JsonProperty("device")
    private String device;
}
