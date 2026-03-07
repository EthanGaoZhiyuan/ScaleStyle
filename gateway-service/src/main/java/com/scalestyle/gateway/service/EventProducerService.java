package com.scalestyle.gateway.service;

import com.scalestyle.gateway.dto.ClickEvent;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;
import org.springframework.stereotype.Service;

import java.util.concurrent.CompletableFuture;

/**
 * Event Producer Service for Kafka streaming
 * 
 * Week 2: Real-time behavior loop
 * Publishes user interaction events to Kafka topics
 */
@Service
@Slf4j
@RequiredArgsConstructor
public class EventProducerService {
    
    private static final String CLICK_TOPIC = "scalestyle.clicks";
    private static final String IMPRESSION_TOPIC = "scalestyle.impressions";
    
    private final KafkaTemplate<String, Object> kafkaTemplate;
    
    /**
     * Publish click event to Kafka (async)
     * 
     * @param event ClickEvent to publish
     * @return CompletableFuture with send result
     */
    public CompletableFuture<SendResult<String, Object>> publishClickEvent(ClickEvent event) {
        if (event == null || event.getUserId() == null || event.getItemId() == null) {
            log.error("Invalid click event: userId and itemId are required");
            return CompletableFuture.failedFuture(new IllegalArgumentException("Invalid event"));
        }
        
        // Use userId as partition key for ordering per user
        String key = event.getUserId();
        
        log.info("Publishing click event: userId={}, itemId={}, source={}, query={}", 
                event.getUserId(), event.getItemId(), event.getSource(), event.getQuery());
        
        CompletableFuture<SendResult<String, Object>> future = kafkaTemplate.send(CLICK_TOPIC, key, event);
        
        // Async callback for logging (non-blocking)
        future.whenComplete((result, ex) -> {
            if (ex != null) {
                log.error("Failed to send click event: {}", event, ex);
            } else {
                log.debug("Click event sent successfully: partition={}, offset={}", 
                        result.getRecordMetadata().partition(), 
                        result.getRecordMetadata().offset());
            }
        });
        
        return future;
    }
    
    /**
     * Publish impression event to Kafka (future use)
     * 
     * @param userId user ID
     * @param itemIds list of item IDs shown
     * @param sessionId session ID
     * @param source source of impression
     * @return CompletableFuture with send result
     */
    public CompletableFuture<SendResult<String, Object>> publishImpressionEvent(
            String userId, java.util.List<String> itemIds, String sessionId, String source) {
        
        // TODO: Implement impression event schema
        log.warn("Impression events not yet implemented (Week 2 stretch goal)");
        return CompletableFuture.completedFuture(null);
    }
}
