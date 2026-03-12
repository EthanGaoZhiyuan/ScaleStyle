package com.scalestyle.gateway.handler;

import com.scalestyle.gateway.exception.BusinessValidationException;
import com.scalestyle.gateway.exception.CommonApiResponse;
import com.scalestyle.gateway.exception.EventTrackingUnavailableException;
import com.scalestyle.gateway.exception.InferenceServiceException;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.resource.NoResourceFoundException;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import jakarta.validation.ConstraintViolationException;
import java.util.stream.Collectors;

/**
 * Global exception handler with secure error responses.
 * 
 * DESIGN PRINCIPLES:
 * 1. Never expose internal implementation details to clients
 * 2. Return stable error codes and messages
 * 3. Include traceId for error correlation
 * 4. Log detailed errors internally for debugging
 * 
 * CRITICAL: HTTP status code must reflect the actual error type.
 * Do NOT return 200 OK with error body - this breaks:
 * - Upstream retry logic
 * - Load balancer health checks
 * - API gateway circuit breakers
 * - Client-side error handling
 * - Observability error rates
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger logger = LoggerFactory.getLogger(GlobalExceptionHandler.class);
    
    /**
     * Handle business validation errors (4xx)
     * These errors have client-safe messages designed for external consumption
     */
    @ExceptionHandler(BusinessValidationException.class)
    public ResponseEntity<CommonApiResponse<String>> handleBusinessValidation(BusinessValidationException e) {
        String traceId = currentTraceId();
        logger.warn("[trace_id={}] Business validation failed: {}", traceId, e.getMessage());
        return ResponseEntity
                .status(HttpStatus.BAD_REQUEST)
                .body(CommonApiResponse.error(400, e.getMessage() + " (traceId: " + traceId + ")"));
    }
    
    /**
     * Handle generic IllegalArgumentException (4xx)
     * These might contain internal details - use generic message
     */
    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<CommonApiResponse<String>> handleIllegalArgument(IllegalArgumentException e) {
        String traceId = currentTraceId();
        // Log full details internally
        logger.warn("[trace_id={}] Invalid request: {}", traceId, e.getMessage(), e);
        // Return generic message to client (don't leak internal details)
        return ResponseEntity
                .status(HttpStatus.BAD_REQUEST)
                .body(CommonApiResponse.error(400, "Invalid request parameters. (traceId: " + traceId + ")"));
    }
    
    /**
     * Handle Bean Validation errors (4xx)
     * Validation messages are designed to be client-safe
     */
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<CommonApiResponse<String>> handleValidationErrors(MethodArgumentNotValidException e) {
        String traceId = currentTraceId();
        String errors = e.getBindingResult()
                .getFieldErrors()
                .stream()
                .map(FieldError::getDefaultMessage)
                .collect(Collectors.joining("; "));
        logger.warn("[trace_id={}] Validation failed: {}", traceId, errors);
        return ResponseEntity
                .status(HttpStatus.BAD_REQUEST)
                .body(CommonApiResponse.error(400, "Validation failed: " + errors + " (traceId: " + traceId + ")"));
    }
    
    /**
     * Handle constraint violation errors (4xx)
     * For @RequestParam validation
     */
    @ExceptionHandler(ConstraintViolationException.class)
    public ResponseEntity<CommonApiResponse<String>> handleConstraintViolation(ConstraintViolationException e) {
        String traceId = currentTraceId();
        String errors = e.getConstraintViolations()
                .stream()
                .map(violation -> violation.getMessage())
                .collect(Collectors.joining("; "));
        logger.warn("[trace_id={}] Constraint violation: {}", traceId, errors);
        return ResponseEntity
                .status(HttpStatus.BAD_REQUEST)
                .body(CommonApiResponse.error(400, "Invalid parameters: " + errors + " (traceId: " + traceId + ")"));
    }

    @ExceptionHandler(MissingServletRequestParameterException.class)
    public ResponseEntity<CommonApiResponse<String>> handleMissingParams(MissingServletRequestParameterException e) {
        String traceId = currentTraceId();
        logger.warn("[trace_id={}] Missing parameter: {}", traceId, e.getParameterName());
        return ResponseEntity
                .status(HttpStatus.BAD_REQUEST)
                .body(CommonApiResponse.error(400, "Required parameter missing: " + e.getParameterName() + " (traceId: " + traceId + ")"));
    }

    /**
     * Handle downstream service failures (503)
     * Never expose internal service details, endpoints, or stack traces
     */
    @ExceptionHandler(InferenceServiceException.class)
    public ResponseEntity<CommonApiResponse<String>> handleInferenceServiceException(InferenceServiceException e) {
        String traceId = currentTraceId();
        // Log full details internally (including cause chain)
        logger.error("[trace_id={}] Inference service error", traceId, e);
        // Return generic message to client
        return ResponseEntity
                .status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(CommonApiResponse.error(503, "Recommendation service temporarily unavailable. (traceId: " + traceId + ")"));
    }

    @ExceptionHandler(EventTrackingUnavailableException.class)
    public ResponseEntity<CommonApiResponse<String>> handleEventTrackingUnavailable(EventTrackingUnavailableException e) {
        String traceId = currentTraceId();
        logger.error("[trace_id={}] Event tracking pipeline unavailable", traceId, e);
        return ResponseEntity
                .status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(CommonApiResponse.error(503, "Event tracking temporarily unavailable. (traceId: " + traceId + ")"));
    }

    /**
     * Handle resource not found (404)
     * Don't expose full resource paths
     */
    @ExceptionHandler(NoResourceFoundException.class)
    public ResponseEntity<CommonApiResponse<String>> handleNoResourceFoundException(NoResourceFoundException ex) {
        String traceId = currentTraceId();
        // Log full path internally
        logger.warn("[trace_id={}] Resource not found: {}", traceId, ex.getResourcePath());
        // Return generic message (don't expose full path)
        return ResponseEntity
                .status(HttpStatus.NOT_FOUND)
                .body(CommonApiResponse.error(404, "Resource not found. (traceId: " + traceId + ")"));
    }

    /**
     * Handle runtime exceptions (500)
     * Never expose implementation details, stack traces, or internal state
     */
    @ExceptionHandler(RuntimeException.class)
    public ResponseEntity<CommonApiResponse<String>> handleRuntimeException(RuntimeException e) {
        String traceId = currentTraceId();
        // Log full exception details internally
        logger.error("[trace_id={}] Runtime exception caught", traceId, e);
        // Return generic message to client
        return ResponseEntity
                .status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(CommonApiResponse.error(500, "Internal server error. (traceId: " + traceId + ")"));
    }

    /**
     * Catch-all handler for unknown exceptions (500)
     * Last line of defense - never leak internal details
     */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<CommonApiResponse<String>> handleException(Exception ex) {
        String traceId = currentTraceId();
        // Log full exception details internally
        logger.error("[trace_id={}] Unhandled exception caught", traceId, ex);
        // Return generic message to client
        return ResponseEntity
                .status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(CommonApiResponse.error(500, "Internal server error. (traceId: " + traceId + ")"));
    }
    
    /**
     * Returns the W3C trace-id from the active OpenTelemetry span so that
     * error responses can be correlated in Jaeger / the OTel backend.
     *
     * When no span is active (unit tests, health-check paths, pre-instrumentation
     * code paths) returns the explicit sentinel {@code "trace-unavailable"}.
     *
     * We do NOT generate a random pseudo-trace-ID as a fallback.  A fabricated ID
     * cannot be looked up in any tracing backend, creates false uniqueness signals,
     * and misleads operators into thinking a real trace exists.
     */
    private String currentTraceId() {
        SpanContext ctx = Span.current().getSpanContext();
        return ctx.isValid() ? ctx.getTraceId() : "trace-unavailable";
    }
}
