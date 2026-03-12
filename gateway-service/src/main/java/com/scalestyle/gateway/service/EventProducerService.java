package com.scalestyle.gateway.service;

import com.scalestyle.gateway.dto.ClickEventV1;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import lombok.extern.slf4j.Slf4j;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.util.concurrent.CompletableFuture;

/**
 * Kafka publisher for broker-acknowledged click tracking.
 *
 * <p>The underlying Kafka producer uses bounded retries and a finite delivery
 * timeout. Transient broker failures still get a few safe idempotent retries,
 * but terminal publish failure becomes visible within a short operational window
 * instead of hiding behind effectively unbounded retry tail latency.
 */
@Service
@Slf4j
public class EventProducerService {
    private static final String TRACEPARENT_HEADER = "traceparent";
    private static final String TRACESTATE_HEADER = "tracestate";

    @Value("${kafka.topics.clicks:scalestyle.clicks}")
    private String clickTopic;

    private final KafkaTemplate<String, ClickEventV1> kafkaTemplate;
    private final EventTrackingAttemptMetrics eventTrackingAttemptMetrics;

    public EventProducerService(
            KafkaTemplate<String, ClickEventV1> kafkaTemplate,
            EventTrackingAttemptMetrics eventTrackingAttemptMetrics) {
        this.kafkaTemplate = kafkaTemplate;
        this.eventTrackingAttemptMetrics = eventTrackingAttemptMetrics;
    }

    /**
     * Publish the event and return the Kafka completion future.
     */
    public CompletableFuture<SendResult<String, ClickEventV1>> publishClick(ClickEventV1 event) {
        if (event == null || event.getUserId() == null || event.getItemId() == null) {
            log.error("Invalid click event: userId and itemId are required");
            return CompletableFuture.failedFuture(new IllegalArgumentException("Invalid event"));
        }

        String key = event.getUserId();
        log.debug("Publishing click event to Kafka: event_id={}, user_id={}, item_id={}, topic={}, key={}",
                event.getEventId(), event.getUserId(), event.getItemId(), clickTopic, key);

        try {
            ProducerRecord<String, ClickEventV1> record = new ProducerRecord<>(clickTopic, key, event);
            appendTracingHeaders(record);
            CompletableFuture<SendResult<String, ClickEventV1>> future = kafkaTemplate.send(record);
            future.whenComplete((result, ex) -> {
                if (ex != null) {
                    eventTrackingAttemptMetrics.recordKafkaPublishRejected();
                    log.warn("Click event was not acknowledged by broker: event_id={}, user_id={}, item_id={}, topic={}, key={}, error={}",
                            event.getEventId(), event.getUserId(), event.getItemId(), clickTopic, key, ex.getMessage(), ex);
                } else {
                    eventTrackingAttemptMetrics.recordKafkaPublishAcknowledged();
                    log.debug("Click event acknowledged by broker: event_id={}, user_id={}, partition={}, offset={}",
                            event.getEventId(), event.getUserId(),
                            result.getRecordMetadata().partition(),
                            result.getRecordMetadata().offset());
                }
            });
            return future;
        } catch (Exception e) {
            eventTrackingAttemptMetrics.recordKafkaSendBlocked();
            log.error("Kafka send() failed before broker acknowledgment: event_id={}, user_id={}, error={}",
                    event.getEventId(), event.getUserId(), e.getMessage(), e);
            return CompletableFuture.failedFuture(e);
        }
    }

    private void appendTracingHeaders(ProducerRecord<String, ClickEventV1> record) {
        SpanContext spanContext = Span.current().getSpanContext();
        if (!spanContext.isValid()) {
            return;
        }
        String traceparent = "00-" + spanContext.getTraceId() + "-" + spanContext.getSpanId()
                + "-" + spanContext.getTraceFlags().asHex();
        record.headers().add(TRACEPARENT_HEADER, traceparent.getBytes(StandardCharsets.UTF_8));
        String tracestate = spanContext.getTraceState().toString();
        if (!tracestate.isBlank()) {
            record.headers().add(TRACESTATE_HEADER, tracestate.getBytes(StandardCharsets.UTF_8));
        }
    }
}
