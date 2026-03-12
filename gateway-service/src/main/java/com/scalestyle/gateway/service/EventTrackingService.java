package com.scalestyle.gateway.service;

import com.scalestyle.gateway.dto.ClickEventV1;
import com.scalestyle.gateway.dto.ClickEventResponse;
import com.scalestyle.gateway.dto.EventSource;
import com.scalestyle.gateway.dto.TrackClickCommand;
import com.scalestyle.gateway.dto.TrackClickRequest;
import com.scalestyle.gateway.exception.BusinessValidationException;
import com.scalestyle.gateway.exception.EventTrackingUnavailableException;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import org.springframework.beans.factory.annotation.Value;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import org.springframework.kafka.support.SendResult;
import java.time.Instant;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * Business logic for broker-acknowledged click event tracking.
 * Handles validation, normalization, and orchestration.
 */
@Service
@Slf4j
@RequiredArgsConstructor
public class EventTrackingService {

    private final EventProducerService eventProducerService;
    private final EventTrackingAttemptMetrics eventTrackingAttemptMetrics;

    @Value("${event.producer.kafka.ack-timeout-ms:6000}")
    private long brokerAckTimeoutMs;

    private static final Set<String> VALID_SOURCES = EventSource.ALLOWED;

    private static final Set<String> VALID_DEVICES = Set.of(
        "web", "mobile", "api"
    );

    private static final int MAX_QUERY_LENGTH = 500;
    private static final int MAX_IMAGE_HASH_LENGTH = 128;
    private static final int MAX_POSITION = 10000;

    public ClickEventResponse trackClick(TrackClickRequest request) {
        TrackClickCommand command = toCommand(request);
        validateRequiredFields(command);
        TrackClickCommand normalizedCommand = normalizeCommand(command);
        ClickEventV1 event = toClickEventV1(normalizedCommand);
        String traceId = currentTraceId();

        boolean kafkaSendBlocked = eventTrackingAttemptMetrics.isKafkaSendBlocked();
        boolean asyncPublishDegraded = eventTrackingAttemptMetrics.isAsyncPublishDegraded();

        if (kafkaSendBlocked || asyncPublishDegraded) {
            String reason = kafkaSendBlocked ? "kafka_send_blocked"
                : "async_publish_degraded";
            log.warn("Click tracking blocked before publish: reason={}, trace_id={}, user_id={}, item_id={}",
                    reason, traceId, event.getUserId(), event.getItemId());
            throw new EventTrackingUnavailableException(
                    "Event tracking temporarily unavailable [" + reason + "]", null);
        }

        try {
            SendResult<String, ClickEventV1> result = eventProducerService.publishClick(event)
                .get(brokerAckTimeoutMs, TimeUnit.MILLISECONDS);
            log.info("Click event acknowledged by broker: event_id={}, trace_id={}, user_id={}, partition={}, offset={}",
                event.getEventId(), traceId, event.getUserId(),
                result.getRecordMetadata().partition(),
                result.getRecordMetadata().offset());
        } catch (TimeoutException e) {
            throw new EventTrackingUnavailableException(
                "Event tracking temporarily unavailable [broker_ack_timeout]", e);
        } catch (ExecutionException e) {
            throw new EventTrackingUnavailableException(
                "Event tracking temporarily unavailable [kafka_publish_failed]", e.getCause() == null ? e : e.getCause());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new EventTrackingUnavailableException(
                "Event tracking temporarily unavailable [interrupted]", e);
        }

        return ClickEventResponse.builder()
                .eventId(event.getEventId())
            .status("acknowledged_by_broker")
            .message("Click event acknowledged by Kafka broker.")
            .processingMode("broker_ack_sync")
                .build();
    }

    private TrackClickCommand toCommand(TrackClickRequest request) {
        if (request == null) {
            throw new BusinessValidationException("Request body is required");
        }
        return new TrackClickCommand(
                request.getUserId(),
                request.getItemId(),
                request.getSessionId(),
                request.getSource(),
                request.getQuery(),
                request.getImageHash(),
                request.getPosition(),
                request.getDevice()
        );
    }

    private void validateRequiredFields(TrackClickCommand command) {
        if (!StringUtils.hasText(command.source())) {
            throw new BusinessValidationException("source is required");
        }
        // Normalize to lowercase before the allowlist check so that "SEARCH" and "search"
        // are treated consistently. Full normalization (trim, etc.) still happens in
        // normalizeCommand(); this lowercase is only for the contains() comparison.
        String normalizedSource = command.source().toLowerCase();
        if (!VALID_SOURCES.contains(normalizedSource)) {
            throw new BusinessValidationException(
                "Invalid source. Must be one of: " + String.join(", ", VALID_SOURCES));
        }

        if (command.userId() == null || command.userId().trim().isEmpty()) {
            throw new BusinessValidationException("Missing or empty required field: user_id");
        }

        if (command.itemId() == null || command.itemId().trim().isEmpty()) {
            throw new BusinessValidationException("Missing or empty required field: item_id");
        }

        if (command.query() != null && command.query().length() > MAX_QUERY_LENGTH) {
            throw new BusinessValidationException("query exceeds maximum length of " + MAX_QUERY_LENGTH);
        }

        if (command.imageHash() != null && command.imageHash().length() > MAX_IMAGE_HASH_LENGTH) {
            throw new BusinessValidationException("image_hash exceeds maximum length of " + MAX_IMAGE_HASH_LENGTH);
        }

        if (command.position() != null && command.position() < 0) {
            throw new BusinessValidationException("position must be non-negative");
        }

        if (command.position() != null && command.position() > MAX_POSITION) {
            throw new BusinessValidationException("position exceeds maximum value of " + MAX_POSITION);
        }
    }

    private TrackClickCommand normalizeCommand(TrackClickCommand command) {
        String source = command.source().toLowerCase();

        String normalizedDevice;
        if (command.device() == null) {
            normalizedDevice = "api";
        } else {
            String device = command.device().toLowerCase();
            if (!VALID_DEVICES.contains(device)) {
                throw new BusinessValidationException(
                    "Invalid device. Must be one of: " + String.join(", ", VALID_DEVICES)
                );
            }
            normalizedDevice = device;
        }

        return new TrackClickCommand(
                command.userId(),
            normalizeItemId(command.itemId()),
                command.sessionId(),
                source,
                command.query(),
                command.imageHash(),
                command.position(),
                normalizedDevice
        );
    }

    private ClickEventV1 toClickEventV1(TrackClickCommand command) {
        Instant now = Instant.now();
        return ClickEventV1.builder()
                .eventType("click")
                .eventId(UUID.randomUUID().toString())
                .userId(command.userId())
                .itemId(command.itemId())
                .timestamp(now.toString())
                .eventTimestampMs(now.toEpochMilli())
                .sessionId(command.sessionId())
                .source(command.source())
                .query(command.query())
                .imageHash(command.imageHash())
                .position(command.position())
                .device(command.device())
                .build();
    }

    private String normalizeItemId(String itemId) {
        if (itemId == null) {
            return null;
        }
        String trimmed = itemId.trim();
        if (trimmed.matches("\\d+")) {
            return String.format("%010d", Long.parseLong(trimmed));
        }
        return trimmed;
    }

    private String currentTraceId() {
        SpanContext spanContext = Span.current().getSpanContext();
        return spanContext.isValid() ? spanContext.getTraceId() : "trace-unavailable";
    }
}
