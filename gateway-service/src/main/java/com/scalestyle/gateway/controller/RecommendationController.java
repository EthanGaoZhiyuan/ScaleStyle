package com.scalestyle.gateway.controller;

import com.scalestyle.gateway.dto.ImageSearchRequest;
import com.scalestyle.gateway.dto.ImageSearchResponse;
import com.scalestyle.gateway.dto.RecommendationDTO;
import com.scalestyle.gateway.exception.CommonApiResponse;
import com.scalestyle.gateway.exception.InferenceServiceException;
import com.scalestyle.gateway.service.RecommendationService;
import io.swagger.v3.oas.annotations.tags.Tag;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.context.request.async.DeferredResult;

import jakarta.validation.Valid;
import jakarta.validation.constraints.Size;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.Max;

import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;

@Slf4j
@RestController
@RequiredArgsConstructor
@Validated
@Tag(name = "Recommendation", description = "Core recommendation endpoints")
public class RecommendationController {

    /**
     * Outer deadline for recommendation requests, in milliseconds.
     *
     * <p>Chosen to be larger than every inner timeout layer so that the
     * DeferredResult never fires before the application-level or Netty
     * timeouts have had a chance to produce their own error signal:
     * <ul>
     *   <li>350 ms — RecommendationService application timeout (Mono.timeout)
     *   <li>450 ms — Netty response/read timeout (hard socket kill)
     *   <li>600 ms — this DeferredResult deadline (catches anything that slips
     *       past the inner layers, e.g. a stuck executor thread)
     * </ul>
     */
    private static final long DEFERRED_TIMEOUT_MS = 600L;

    private final RecommendationService recommendationService;

    @GetMapping("/api/recommendation/debug/cache-stats")
    public Map<String, Object> cacheStats() {
        return recommendationService.getCacheStats();
    }

    /**
     * Text-query recommendation search.
     *
     * <p>Uses {@link DeferredResult} so the Tomcat request thread is released
     * immediately after the async work is dispatched.  Three hooks enforce the
     * "no zombie request" contract:
     * <ol>
     *   <li>{@code onTimeout} — fires when no result has been set within
     *       {@value #DEFERRED_TIMEOUT_MS} ms; cancels the in-flight future and
     *       returns a 503 to the client.
     *   <li>{@code onCompletion} — fires whenever the DeferredResult is resolved
     *       (success, error, or timeout); cancels the future if it is still
     *       running (covers the client-disconnect path where Spring completes
     *       the DeferredResult without going through onTimeout).
     *   <li>{@code whenComplete} on the future — propagates normal results and
     *       exceptions back to the DeferredResult.
     * </ol>
     */
    @GetMapping({"/api/recommendation/search", "/search"})
    public DeferredResult<ResponseEntity<CommonApiResponse<List<RecommendationDTO>>>> search(
            @RequestParam @Size(min = 1, max = 500, message = "query must be 1-500 characters") String query,
            @RequestParam(required = false) @Size(max = 100, message = "userId must not exceed 100 characters") String userId,
            @RequestParam(required = false, defaultValue = "search") String intent,
            @RequestParam(defaultValue = "10") @Min(1) @Max(100) Integer k,
            @RequestParam(defaultValue = "false") Boolean debug
    ) {
        DeferredResult<ResponseEntity<CommonApiResponse<List<RecommendationDTO>>>> deferredResult =
                new DeferredResult<>(DEFERRED_TIMEOUT_MS);

        CompletableFuture<List<RecommendationDTO>> future =
                recommendationService.searchAsync(query, userId, intent, k, debug);

        deferredResult.onTimeout(() -> {
            future.cancel(true);
            deferredResult.setErrorResult(new InferenceServiceException("Recommendation request timed out"));
        });

        // Fires on normal completion, timeout, or client disconnect.
        // Cancels the future when it is still running to prevent zombie requests
        // continuing to consume Ray/Redis resources after the client is gone.
        deferredResult.onCompletion(() -> {
            if (!future.isDone()) {
                future.cancel(true);
            }
        });

        future.whenComplete((result, throwable) -> {
            if (throwable != null) {
                log.error("Async search failed", throwable);
                deferredResult.setErrorResult(throwable);
            } else {
                deferredResult.setResult(ResponseEntity.ok(CommonApiResponse.success(result)));
            }
        });

        return deferredResult;
    }

    /**
     * Image-based recommendation search (text-to-image or image-to-image).
     *
     * <p>Applies the same timeout / cancellation contract as {@link #search}.
     */
    @PostMapping({"/api/recommendation/search/image", "/search/image"})
    public DeferredResult<ResponseEntity<CommonApiResponse<ImageSearchResponse>>> imageSearch(
            @Valid @RequestBody ImageSearchRequest request) {

        DeferredResult<ResponseEntity<CommonApiResponse<ImageSearchResponse>>> deferredResult =
                new DeferredResult<>(DEFERRED_TIMEOUT_MS);

        CompletableFuture<ImageSearchResponse> future =
                recommendationService.imageSearchAsync(request);

        deferredResult.onTimeout(() -> {
            future.cancel(true);
            deferredResult.setErrorResult(new InferenceServiceException("Image search request timed out"));
        });

        deferredResult.onCompletion(() -> {
            if (!future.isDone()) {
                future.cancel(true);
            }
        });

        future.whenComplete((result, throwable) -> {
            if (throwable != null) {
                log.error("Async image search failed", throwable);
                deferredResult.setErrorResult(throwable);
            } else {
                deferredResult.setResult(ResponseEntity.ok(CommonApiResponse.success(result)));
            }
        });

        return deferredResult;
    }
}
