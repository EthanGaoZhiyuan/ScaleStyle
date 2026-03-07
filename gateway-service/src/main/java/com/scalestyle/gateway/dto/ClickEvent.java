package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.Instant;

/**
 * Click Event DTO for Kafka event streaming
 * 
 * Schema: docs/contracts/events.md
 * Topic: scalestyle.clicks
 * 
 * Week 2: Real-time behavior loop
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class ClickEvent {
    
    @JsonProperty("event_type")
    @Builder.Default
    private String eventType = "click";
    
    @JsonProperty("event_id")
    private String eventId;
    
    @JsonProperty("user_id")
    private String userId;
    
    @JsonProperty("item_id")
    private String itemId;
    
    @JsonProperty("timestamp")
    private String timestamp;
    
    @JsonProperty("session_id")
    private String sessionId;
    
    @JsonProperty("source")
    private String source; // "search" | "browse" | "recommendation" | "image_search"
    
    @JsonProperty("query")
    private String query; // Optional
    
    @JsonProperty("image_hash")
    private String imageHash; // Optional, for image search
    
    @JsonProperty("position")
    private Integer position; // Item position in results (0-indexed)
    
    @JsonProperty("device")
    private String device; // "web" | "mobile" | "api"
    
    /**
     * Create a ClickEvent with current timestamp
     */
    public static ClickEvent create(String userId, String itemId, String sessionId, 
                                    String source, String query, Integer position) {
        return ClickEvent.builder()
                .eventId(java.util.UUID.randomUUID().toString())
                .userId(userId)
                .itemId(itemId)
                .timestamp(Instant.now().toString())
                .sessionId(sessionId)
                .source(source)
                .query(query)
                .position(position)
                .device("api")
                .build();
    }
}
