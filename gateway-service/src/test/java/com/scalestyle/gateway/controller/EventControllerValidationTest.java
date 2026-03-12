package com.scalestyle.gateway.controller;

import com.scalestyle.gateway.dto.ClickEventResponse;
import com.scalestyle.gateway.dto.TrackClickRequest;
import com.scalestyle.gateway.exception.CommonApiResponse;
import com.scalestyle.gateway.exception.EventTrackingUnavailableException;
import com.scalestyle.gateway.service.EventTrackingAttemptMetrics;
import com.scalestyle.gateway.service.EventTrackingService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.context.request.async.DeferredResult;

import java.lang.reflect.Field;
import java.util.concurrent.Executor;
import java.util.concurrent.RejectedExecutionException;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Controller tests after contract-boundary refactor.
 *
 * Controller responsibilities are intentionally narrow:
 * - Deserialize request body
 * - Delegate to service layer
 * - Return HTTP 200 wrapper after Kafka broker acknowledgment
 *
 * Validation and error mapping happen in service + global exception handler.
 */
@ExtendWith(MockitoExtension.class)
class EventControllerValidationTest {

    @Mock
    private EventTrackingService eventTrackingService;

    @Mock
    private EventTrackingAttemptMetrics eventTrackingAttemptMetrics;

    private EventController eventController;
    private Executor directExecutor;

    private TrackClickRequest validRequest;

    @BeforeEach
    void setUp() {
        directExecutor = Runnable::run;
        eventController = new EventController(eventTrackingService, eventTrackingAttemptMetrics, directExecutor);

        validRequest = TrackClickRequest.builder()
                .userId("user_12345")
                .itemId("108775051")
                .sessionId("sess_abc123")
                .source("search")
                .query("red dress")
                .position(2)
                .device("web")
                .build();
    }

    @Test
    void trackClick_shouldReturn200_withBrokerAckContract_whenServiceSucceeds() {
        ClickEventResponse serviceResponse = ClickEventResponse.builder()
                .eventId("evt_1")
                .status("acknowledged_by_broker")
                .message("Click event acknowledged by Kafka broker.")
                .processingMode("broker_ack_sync")
                .build();
        when(eventTrackingService.trackClick(any(TrackClickRequest.class))).thenReturn(serviceResponse);

        DeferredResult<ResponseEntity<CommonApiResponse<ClickEventResponse>>> deferredResult =
            eventController.trackClick(validRequest);

        assertTrue(deferredResult.hasResult());
        @SuppressWarnings("unchecked")
        ResponseEntity<CommonApiResponse<ClickEventResponse>> entity =
            (ResponseEntity<CommonApiResponse<ClickEventResponse>>) deferredResult.getResult();

        assertEquals(HttpStatus.OK, entity.getStatusCode());
        CommonApiResponse<ClickEventResponse> body = entity.getBody();
        assertNotNull(body);
        assertEquals(200, body.getCode());
        assertSame(serviceResponse, body.getData());
        assertEquals("acknowledged_by_broker", body.getData().getStatus());
        assertEquals("broker_ack_sync", body.getData().getProcessingMode());

        verify(eventTrackingService).trackClick(validRequest);
        verify(eventTrackingAttemptMetrics).recordLocalProcessingAccepted();
    }

    @Test
    void trackClick_shouldExposeAsyncError_forGlobalHandler() {
        IllegalArgumentException exception = new IllegalArgumentException("Missing required field: source");
        when(eventTrackingService.trackClick(any(TrackClickRequest.class))).thenThrow(exception);

        DeferredResult<ResponseEntity<CommonApiResponse<ClickEventResponse>>> deferredResult =
            eventController.trackClick(validRequest);

        assertTrue(deferredResult.hasResult());
        assertSame(exception, deferredResult.getResult());
        verify(eventTrackingService).trackClick(validRequest);
        verify(eventTrackingAttemptMetrics).recordLocalProcessingAccepted();
    }

    @Test
    void trackClick_shouldConfigureControllerTimeout_andSurfaceUnavailableOnTimeout() throws Exception {
        Executor blockingExecutor = command -> {
        };
        eventController = new EventController(eventTrackingService, eventTrackingAttemptMetrics, blockingExecutor);

        DeferredResult<ResponseEntity<CommonApiResponse<ClickEventResponse>>> deferredResult =
                eventController.trackClick(validRequest);

        assertEquals(6500L, getTimeoutValue(deferredResult));
        assertNull(deferredResult.getResult());

        getTimeoutCallback(deferredResult).run();

        assertTrue(deferredResult.hasResult());
        EventTrackingUnavailableException error =
                assertInstanceOf(EventTrackingUnavailableException.class, deferredResult.getResult());
        assertEquals("Event tracking temporarily unavailable [request_timeout]", error.getMessage());
        verify(eventTrackingAttemptMetrics).recordLocalProcessingAccepted();
        verify(eventTrackingService, never()).trackClick(any());
    }

    @Test
    void trackClick_shouldRejectWhenLocalExecutorIsSaturated() {
        Executor rejectingExecutor = command -> {
            throw new RejectedExecutionException("queue full");
        };
        eventController = new EventController(eventTrackingService, eventTrackingAttemptMetrics, rejectingExecutor);

        DeferredResult<ResponseEntity<CommonApiResponse<ClickEventResponse>>> deferredResult =
            eventController.trackClick(validRequest);

        assertTrue(deferredResult.hasResult());
        EventTrackingUnavailableException error =
            assertInstanceOf(EventTrackingUnavailableException.class, deferredResult.getResult());
        assertEquals("Event tracking temporarily unavailable [local_queue_rejected]", error.getMessage());
        verify(eventTrackingAttemptMetrics).recordLocalQueueRejected();
        verify(eventTrackingService, never()).trackClick(any());
    }

    @Test
    void health_shouldReturnHealthy() {
        CommonApiResponse<String> response = eventController.health();
        assertEquals(200, response.getCode());
        assertEquals("healthy", response.getData());
    }

    private Runnable getTimeoutCallback(DeferredResult<?> deferredResult) throws Exception {
        Field timeoutCallbackField = DeferredResult.class.getDeclaredField("timeoutCallback");
        timeoutCallbackField.setAccessible(true);
        return (Runnable) timeoutCallbackField.get(deferredResult);
    }

    private Long getTimeoutValue(DeferredResult<?> deferredResult) throws Exception {
        Field timeoutValueField = DeferredResult.class.getDeclaredField("timeoutValue");
        timeoutValueField.setAccessible(true);
        return (Long) timeoutValueField.get(deferredResult);
    }
}
