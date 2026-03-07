package com.scalestyle.gateway.controller;

import com.scalestyle.gateway.dto.ClickEvent;
import com.scalestyle.gateway.exception.CommonApiResponse;
import com.scalestyle.gateway.service.EventProducerService;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * Event Controller for user interaction tracking
 * 
 * Week 2: Real-time behavior loop
 * Exposes POST /events/click for capturing user clicks
 */
@RestController
@RequestMapping("/api/events")
@RequiredArgsConstructor
@Slf4j
@Tag(name = "Events", description = "User interaction event tracking")
public class EventController {
    
    private final EventProducerService eventProducerService;
    
    /**
     * POST /api/events/click
     * 
     * Capture user click event and publish to Kafka
     * 
     * Request body example:
     * {
     *   "user_id": "user_12345",
     *   "item_id": "108775051",
     *   "session_id": "sess_abc123",
     *   "source": "search",
     *   "query": "red dress",
     *   "position": 2
     * }
     */
    @PostMapping("/click")
    @Operation(summary = "Track user click event", description = "Publish click event to Kafka for real-time personalization")
    public CommonApiResponse<Map<String, Object>> trackClick(@RequestBody ClickEvent event) {
        log.info("Received click event: userId={}, itemId={}, source={}", 
                event.getUserId(), event.getItemId(), event.getSource());
        
        // P0.2: Strengthen validation - validate required fields
        if (event.getUserId() == null || event.getUserId().trim().isEmpty()) {
            return CommonApiResponse.error(400, "Missing or empty required field: user_id");
        }
        
        if (event.getItemId() == null || event.getItemId().trim().isEmpty()) {
            return CommonApiResponse.error(400, "Missing or empty required field: item_id");
        }
        
        if (event.getSessionId() == null || event.getSessionId().trim().isEmpty()) {
            return CommonApiResponse.error(400, "Missing or empty required field: session_id");
        }
        
        // P0.2: Validate source against allowlist
        if (event.getSource() == null) {
            event.setSource("unknown");
        } else {
            String source = event.getSource().toLowerCase();
            if (!source.equals("search") && !source.equals("browse") && 
                !source.equals("recommendation") && !source.equals("image_search") && 
                !source.equals("unknown")) {
                return CommonApiResponse.error(400, 
                    "Invalid source. Must be one of: search, browse, recommendation, image_search");
            }
            event.setSource(source);
        }
        
        // P0.2: Validate event_type if provided, or set default
        if (event.getEventType() != null && !event.getEventType().equals("click")) {
            return CommonApiResponse.error(400, "Invalid event_type. Only 'click' is supported");
        }
        if (event.getEventType() == null) {
            event.setEventType("click");
        }
        
        // P0.2: Validate query length if present
        if (event.getQuery() != null && event.getQuery().length() > 500) {
            return CommonApiResponse.error(400, "query field exceeds maximum length of 500 characters");
        }
        
        // P0.2: Validate image_hash length if present
        if (event.getImageHash() != null && event.getImageHash().length() > 128) {
            return CommonApiResponse.error(400, "image_hash field exceeds maximum length of 128 characters");
        }
        
        // P0.2: Validate timestamp if provided, or set to current time
        if (event.getTimestamp() != null) {
            try {
                java.time.Instant.parse(event.getTimestamp());
            } catch (java.time.format.DateTimeParseException e) {
                return CommonApiResponse.error(400, "Invalid timestamp format. Must be ISO-8601");
            }
        } else {
            event.setTimestamp(java.time.Instant.now().toString());
        }
        
        // Set event_id if not provided
        if (event.getEventId() == null) {
            event.setEventId(java.util.UUID.randomUUID().toString());
        }
        
        // P0.4: Validate device against allowlist
        if (event.getDevice() == null) {
            event.setDevice("api");
        } else {
            String device = event.getDevice().toLowerCase();
            if (!device.equals("web") && !device.equals("mobile") && !device.equals("api")) {
                return CommonApiResponse.error(400, 
                    "Invalid device. Must be one of: web, mobile, api");
            }
            event.setDevice(device);
        }
        
        // P0.4: Validate position if provided (non-negative)
        if (event.getPosition() != null && event.getPosition() < 0) {
            return CommonApiResponse.error(400, 
                "Invalid position. Must be non-negative");
        }
        
        // Publish to Kafka (async, non-blocking)
        eventProducerService.publishClickEvent(event);
        
        return CommonApiResponse.success(Map.of(
                "event_id", event.getEventId(),
                "status", "accepted",
                "message", "Click event published to Kafka"
        ));
    }
    
    /**
     * Health check endpoint for event service
     */
    @GetMapping("/health")
    @Operation(summary = "Event service health check")
    public CommonApiResponse<Map<String, String>> health() {
        return CommonApiResponse.success(Map.of(
                "status", "healthy",
                "service", "event-streaming"
        ));
    }
}
