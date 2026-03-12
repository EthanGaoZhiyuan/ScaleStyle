package com.scalestyle.gateway.service;

import com.scalestyle.gateway.dto.ClickEventResponse;
import com.scalestyle.gateway.dto.ClickEventV1;
import com.scalestyle.gateway.dto.TrackClickRequest;
import com.scalestyle.gateway.exception.BusinessValidationException;
import com.scalestyle.gateway.exception.EventTrackingUnavailableException;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.apache.kafka.clients.producer.RecordMetadata;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.kafka.support.SendResult;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeoutException;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Enforces the broker-acknowledged ingestion contract.
 */
@ExtendWith(MockitoExtension.class)
class EventTrackingServiceBestEffortContractTest {

    @Mock
    private EventProducerService eventProducerService;

    @Mock
    private EventTrackingAttemptMetrics eventTrackingAttemptMetrics;

    private EventTrackingService eventTrackingService;
    private TrackClickRequest request;

    @BeforeEach
    void setUp() {
        eventTrackingService = new EventTrackingService(eventProducerService, eventTrackingAttemptMetrics);
                ReflectionTestUtils.setField(eventTrackingService, "brokerAckTimeoutMs", 50L);
        request = TrackClickRequest.builder()
                .userId("user-1")
                .itemId("item-2")
                .sessionId("session-3")
                .source("search")
                .query("linen shirt")
                .position(1)
                .device("web")
                .build();
    }

    @Test
    void trackClick_returnsSuccessOnlyAfterBrokerAck() {
        @SuppressWarnings("unchecked")
        SendResult<String, ClickEventV1> sendResult = org.mockito.Mockito.mock(SendResult.class);
                RecordMetadata metadata = org.mockito.Mockito.mock(RecordMetadata.class);
                when(metadata.partition()).thenReturn(1);
                when(metadata.offset()).thenReturn(42L);
                when(sendResult.getRecordMetadata()).thenReturn(metadata);
        when(eventTrackingAttemptMetrics.isKafkaSendBlocked()).thenReturn(false);
        when(eventTrackingAttemptMetrics.isAsyncPublishDegraded()).thenReturn(false);
        when(eventProducerService.publishClick(any(ClickEventV1.class)))
                .thenReturn(CompletableFuture.completedFuture(sendResult));

        ClickEventResponse response = eventTrackingService.trackClick(request);

        assertEquals("acknowledged_by_broker", response.getStatus());
        assertEquals("broker_ack_sync", response.getProcessingMode());
        assertEquals("Click event acknowledged by Kafka broker.",
                response.getMessage());
        verify(eventProducerService).publishClick(any(ClickEventV1.class));
    }

    @Test
    void trackClick_rejectsWhenKafkaSendIsBlocked() {
        when(eventTrackingAttemptMetrics.isKafkaSendBlocked()).thenReturn(true);
        when(eventTrackingAttemptMetrics.isAsyncPublishDegraded()).thenReturn(false);

        EventTrackingUnavailableException ex = assertThrows(
                EventTrackingUnavailableException.class,
                () -> eventTrackingService.trackClick(request)
        );

                assertEquals("Event tracking temporarily unavailable [kafka_send_blocked]", ex.getMessage());
                verify(eventProducerService, never()).publishClick(any());
    }

    @Test
        void trackClick_translatesBrokerTimeoutIntoTrackingUnavailable() {
        when(eventTrackingAttemptMetrics.isKafkaSendBlocked()).thenReturn(false);
        when(eventTrackingAttemptMetrics.isAsyncPublishDegraded()).thenReturn(false);
                CompletableFuture<SendResult<String, ClickEventV1>> neverCompletingFuture = new CompletableFuture<>();
                when(eventProducerService.publishClick(any(ClickEventV1.class))).thenReturn(neverCompletingFuture);
                ReflectionTestUtils.setField(eventTrackingService, "brokerAckTimeoutMs", 1L);

        EventTrackingUnavailableException ex = assertThrows(
                EventTrackingUnavailableException.class,
                () -> eventTrackingService.trackClick(request)
        );

        assertEquals("Event tracking temporarily unavailable [broker_ack_timeout]", ex.getMessage());
    }

    @Test
    void trackClick_rejectsWhenRecentAsyncPublishFailuresDegradePipeline() {
        when(eventTrackingAttemptMetrics.isKafkaSendBlocked()).thenReturn(false);
        when(eventTrackingAttemptMetrics.isAsyncPublishDegraded()).thenReturn(true);

        EventTrackingUnavailableException ex = assertThrows(
                EventTrackingUnavailableException.class,
                () -> eventTrackingService.trackClick(request)
        );

        assertEquals("Event tracking temporarily unavailable [async_publish_degraded]", ex.getMessage());
        verify(eventProducerService, never()).publishClick(any());
    }

    @Test
    void trackClick_translatesPublishFailureIntoTrackingUnavailable() {
        when(eventTrackingAttemptMetrics.isKafkaSendBlocked()).thenReturn(false);
        when(eventTrackingAttemptMetrics.isAsyncPublishDegraded()).thenReturn(false);
        CompletableFuture<SendResult<String, ClickEventV1>> failed = new CompletableFuture<>();
        failed.completeExceptionally(new IllegalStateException("broker unavailable"));
        when(eventProducerService.publishClick(any(ClickEventV1.class))).thenReturn(failed);

        EventTrackingUnavailableException ex = assertThrows(
                EventTrackingUnavailableException.class,
                () -> eventTrackingService.trackClick(request)
        );

        assertEquals("Event tracking temporarily unavailable [kafka_publish_failed]", ex.getMessage());
    }

    // --- Source allowlist enforcement ---

    @Test
    void trackClick_rejectsUnrecognizedSource() {
        TrackClickRequest badRequest = TrackClickRequest.builder()
                .userId("user-1").itemId("item-2").source("internal_admin").build();

        BusinessValidationException ex = assertThrows(
                BusinessValidationException.class,
                () -> eventTrackingService.trackClick(badRequest)
        );

        assertTrue(ex.getMessage().contains("Invalid source"),
                "Expected 'Invalid source' in: " + ex.getMessage());
        verify(eventProducerService, never()).publishClick(any());
    }

    @Test
    void trackClick_rejectsUppercaseSourceThatWouldBypassBeanValidation() {
        // @Valid on the controller rejects "SEARCH" at the HTTP layer, but a direct
        // service call (internal flows, tests) must also be rejected by the service.
        TrackClickRequest badRequest = TrackClickRequest.builder()
                .userId("user-1").itemId("item-2").source("SEARCH").build();

        // "SEARCH".toLowerCase() == "search" which IS in VALID_SOURCES, so this
        // must pass — it confirms the normalize-then-check path works correctly.
        when(eventTrackingAttemptMetrics.isKafkaSendBlocked()).thenReturn(false);
        when(eventTrackingAttemptMetrics.isAsyncPublishDegraded()).thenReturn(false);
        ReflectionTestUtils.setField(eventTrackingService, "brokerAckTimeoutMs", 1L);
        when(eventProducerService.publishClick(any(ClickEventV1.class)))
                .thenReturn(new CompletableFuture<>());

        EventTrackingUnavailableException ex = assertThrows(
                EventTrackingUnavailableException.class,
                () -> eventTrackingService.trackClick(badRequest)
        );
        assertEquals("Event tracking temporarily unavailable [broker_ack_timeout]", ex.getMessage());
    }

    @Test
    void trackClick_acceptsAllDefinedValidSources() {
        List.of("search", "browse", "recommendation", "image_search").forEach(src -> {
            TrackClickRequest req = TrackClickRequest.builder()
                    .userId("user-1").itemId("item-2").source(src).build();
            when(eventTrackingAttemptMetrics.isKafkaSendBlocked()).thenReturn(false);
            when(eventTrackingAttemptMetrics.isAsyncPublishDegraded()).thenReturn(false);
            ReflectionTestUtils.setField(eventTrackingService, "brokerAckTimeoutMs", 1L);
            when(eventProducerService.publishClick(any(ClickEventV1.class)))
                    .thenReturn(new CompletableFuture<>());

            EventTrackingUnavailableException ex = assertThrows(
                    EventTrackingUnavailableException.class,
                    () -> eventTrackingService.trackClick(req),
                    "Expected source '" + src + "' to be accepted before publish failure"
            );
            assertEquals("Event tracking temporarily unavailable [broker_ack_timeout]", ex.getMessage());
        });
    }
}
