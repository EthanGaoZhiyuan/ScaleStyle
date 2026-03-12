package com.scalestyle.gateway.controller;

import com.scalestyle.gateway.dto.ClickEventResponse;
import com.scalestyle.gateway.dto.TrackClickRequest;
import com.scalestyle.gateway.exception.CommonApiResponse;
import com.scalestyle.gateway.exception.EventTrackingUnavailableException;
import com.scalestyle.gateway.service.EventTrackingAttemptMetrics;
import com.scalestyle.gateway.service.EventTrackingService;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.responses.ApiResponse;
import io.swagger.v3.oas.annotations.responses.ApiResponses;
import io.swagger.v3.oas.annotations.tags.Tag;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.context.request.async.DeferredResult;

import jakarta.validation.Valid;
import java.util.concurrent.Executor;
import java.util.concurrent.RejectedExecutionException;

/**
 * User interaction event tracking endpoint.
 * Captures click events only after Kafka broker acknowledgment.
 *
 * <h3>Validation layers</h3>
 * <ul>
 *   <li><b>Transport validation</b> ({@code @Valid}): schema constraints in {@link TrackClickRequest}.</li>
 *   <li><b>Business validation</b> ({@link com.scalestyle.gateway.service.EventTrackingService}):
 *       source normalization, device allowlist, broker-ack publish contract.
 *       Failures: {@code 400} ({@code BusinessValidationException}) or {@code 503}
 *       ({@code EventTrackingUnavailableException}).</li>
 * </ul>
 */
@RestController
@RequestMapping({"/api/events", "/events"})
@RequiredArgsConstructor
@Slf4j
@Tag(name = "Events", description = "User interaction event tracking")
public class EventController {

    private final EventTrackingService eventTrackingService;
    private final EventTrackingAttemptMetrics eventTrackingAttemptMetrics;
    @Qualifier("eventProducerExecutor")
    private final Executor eventProducerExecutor;
    // Keep outer Spring timeout slightly above the internal broker-ack deadline (6s).
    private static final long DEFERRED_TIMEOUT_MS = 6500L;

    /**
    * Accepts a click event only after the broker acknowledges persistence.
     *
    * <p>The Tomcat request thread is released immediately after admission to the
    * dedicated click-tracking executor. The Kafka broker-ack wait still happens,
    * but it is isolated to that executor rather than consuming servlet threads.
    * If the request exceeds {@value #DEFERRED_TIMEOUT_MS} ms, the endpoint returns
    * {@code EventTrackingUnavailableException("Event tracking temporarily unavailable [request_timeout]")}.
    *
    * <p>Returns <b>HTTP 200 OK</b> only after Kafka acknowledges the publish.
     *
     * @param request validated click event payload
     * @return 200 with body {@code { status: "acknowledged_by_broker",
     *         event_id: "...", processing_mode: "broker_ack_sync" }}
     */
    @PostMapping("/click")
    @Operation(
        summary = "Track user click event",
        description = "Returns success only after Kafka broker acknowledgment."
    )
    @ApiResponses({
        @ApiResponse(responseCode = "200", description = "Acknowledged by Kafka broker"),
        @ApiResponse(responseCode = "400", description = "Validation failure"),
        @ApiResponse(responseCode = "503", description = "Local executor saturated, Kafka publish unavailable, or broker ack timed out")
    })
    public DeferredResult<ResponseEntity<CommonApiResponse<ClickEventResponse>>> trackClick(
            @Valid @RequestBody TrackClickRequest request) {
        DeferredResult<ResponseEntity<CommonApiResponse<ClickEventResponse>>> deferredResult =
                new DeferredResult<>(DEFERRED_TIMEOUT_MS);

        deferredResult.onTimeout(() -> deferredResult.setErrorResult(
            new EventTrackingUnavailableException("Event tracking temporarily unavailable [request_timeout]")
        ));
        try {
            eventProducerExecutor.execute(() -> processTrackClick(request, deferredResult));
            eventTrackingAttemptMetrics.recordLocalProcessingAccepted();
        } catch (RejectedExecutionException e) {
            eventTrackingAttemptMetrics.recordLocalQueueRejected();
            deferredResult.setErrorResult(new EventTrackingUnavailableException(
                    "Event tracking temporarily unavailable [local_queue_rejected]", e));
        }

        return deferredResult;
    }

    private void processTrackClick(
            TrackClickRequest request,
            DeferredResult<ResponseEntity<CommonApiResponse<ClickEventResponse>>> deferredResult) {
        try {
            ClickEventResponse response = eventTrackingService.trackClick(request);
            deferredResult.setResult(ResponseEntity.status(HttpStatus.OK).body(CommonApiResponse.success(response)));
        } catch (Exception e) {
            deferredResult.setErrorResult(e);
        }
    }

    @GetMapping("/health")
    @Operation(summary = "Event service health check")
    public CommonApiResponse<String> health() {
        return CommonApiResponse.success("healthy");
    }
}
