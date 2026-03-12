package com.scalestyle.gateway.handler;

import com.scalestyle.gateway.exception.BusinessValidationException;
import com.scalestyle.gateway.exception.CommonApiResponse;
import com.scalestyle.gateway.exception.EventTrackingUnavailableException;
import com.scalestyle.gateway.exception.InferenceServiceException;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.BeanPropertyBindingResult;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.MissingServletRequestParameterException;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Tests for GlobalExceptionHandler to verify secure error responses.
 * 
 * Key Requirements:
 * 1. Internal details never exposed to clients
 * 2. All responses include traceId
 * 3. Stable error codes and messages
 * 4. Appropriate HTTP status codes
 */
class GlobalExceptionHandlerSecurityTest {

    private GlobalExceptionHandler handler;

    @BeforeEach
    void setup() {
        handler = new GlobalExceptionHandler();
    }

    @Test
    @DisplayName("BusinessValidationException: Returns client-safe message with traceId")
    void testBusinessValidationException() {
        String clientSafeMessage = "Invalid search mode: text_to_image requires query parameter";
        BusinessValidationException ex = new BusinessValidationException(clientSafeMessage);
        
        ResponseEntity<CommonApiResponse<String>> response = handler.handleBusinessValidation(ex);
        
        assertEquals(HttpStatus.BAD_REQUEST, response.getStatusCode());
        assertEquals(400, response.getBody().getCode());
        assertTrue(response.getBody().getMessage().contains(clientSafeMessage), 
            "Client-safe message should be preserved");
        assertTrue(response.getBody().getMessage().contains("traceId:"), 
            "Should include traceId");
    }

    @Test
    @DisplayName("IllegalArgumentException: Never exposes internal details")
    void testIllegalArgumentException_HidesInternalDetails() {
        // Simulate internal error with implementation details
        String internalError = "NullPointerException in KafkaProducer at line 123 " +
                              "connecting to broker kafka-1.internal:9092";
        IllegalArgumentException ex = new IllegalArgumentException(internalError);
        
        ResponseEntity<CommonApiResponse<String>> response = handler.handleIllegalArgument(ex);
        
        assertEquals(HttpStatus.BAD_REQUEST, response.getStatusCode());
        assertEquals(400, response.getBody().getCode());
        
        // Critical: Internal details should NOT appear in response
        assertFalse(response.getBody().getMessage().contains("NullPointerException"));
        assertFalse(response.getBody().getMessage().contains("KafkaProducer"));
        assertFalse(response.getBody().getMessage().contains("kafka-1.internal"));
        assertFalse(response.getBody().getMessage().contains("9092"));
        assertFalse(response.getBody().getMessage().contains("line 123"));
        
        // Should return generic message instead
        assertTrue(response.getBody().getMessage().contains("Invalid request parameters"));
        assertTrue(response.getBody().getMessage().contains("traceId:"));
    }

    @Test
    @DisplayName("InferenceServiceException: Never exposes service internals")
    void testInferenceServiceException_HidesServiceDetails() {
        // Simulate internal service error with endpoint details
        String internalError = "Connection timeout to http://inference-service.internal:8080/v1/search " +
                              "after 450ms, circuit breaker opened, bulkhead queue full (64/64)";
        InferenceServiceException ex = new InferenceServiceException(internalError);
        
        ResponseEntity<CommonApiResponse<String>> response = handler.handleInferenceServiceException(ex);
        
        assertEquals(HttpStatus.SERVICE_UNAVAILABLE, response.getStatusCode());
        assertEquals(503, response.getBody().getCode());
        
        // Critical: Service internals should NOT appear in response
        assertFalse(response.getBody().getMessage().contains("inference-service.internal"));
        assertFalse(response.getBody().getMessage().contains("8080"));
        assertFalse(response.getBody().getMessage().contains("/v1/search"));
        assertFalse(response.getBody().getMessage().contains("450ms"));
        assertFalse(response.getBody().getMessage().contains("circuit breaker"));
        assertFalse(response.getBody().getMessage().contains("bulkhead"));
        assertFalse(response.getBody().getMessage().contains("64/64"));
        
        // Should return generic stable message
        assertTrue(response.getBody().getMessage().contains("Recommendation service temporarily unavailable"));
        assertTrue(response.getBody().getMessage().contains("traceId:"));
    }

    @Test
    @DisplayName("EventTrackingUnavailableException: Never exposes Kafka internals")
    void testEventTrackingUnavailableException_HidesKafkaDetails() {
        // Simulate Kafka error with broker details
        Throwable kafkaError = new RuntimeException("Failed to send to topic scalestyle.clicks " +
                                                   "partition 3 on broker kafka-prod-1.aws.internal:9092 " +
                                                   "with timeout 5000ms, leader not available");
        EventTrackingUnavailableException ex = new EventTrackingUnavailableException(
            "Kafka publish failed", kafkaError);
        
        ResponseEntity<CommonApiResponse<String>> response = handler.handleEventTrackingUnavailable(ex);
        
        assertEquals(HttpStatus.SERVICE_UNAVAILABLE, response.getStatusCode());
        assertEquals(503, response.getBody().getCode());
        
        // Critical: Kafka internals should NOT appear in response
        assertFalse(response.getBody().getMessage().contains("Kafka"));
        assertFalse(response.getBody().getMessage().contains("scalestyle.clicks"));
        assertFalse(response.getBody().getMessage().contains("partition"));
        assertFalse(response.getBody().getMessage().contains("broker"));
        assertFalse(response.getBody().getMessage().contains("kafka-prod-1.aws.internal"));
        assertFalse(response.getBody().getMessage().contains("9092"));
        assertFalse(response.getBody().getMessage().contains("leader not available"));
        
        // Should return generic stable message
        assertTrue(response.getBody().getMessage().contains("Event tracking temporarily unavailable"));
        assertTrue(response.getBody().getMessage().contains("traceId:"));
    }

    @Test
    @DisplayName("RuntimeException: Never exposes stack traces or internal state")
    void testRuntimeException_HidesStackTrace() {
        // Simulate runtime error with stack trace information
        RuntimeException ex = new RuntimeException(
            "Failed to parse JSON at line 42: Unexpected token 'null' in object definition");
        
        ResponseEntity<CommonApiResponse<String>> response = handler.handleRuntimeException(ex);
        
        assertEquals(HttpStatus.INTERNAL_SERVER_ERROR, response.getStatusCode());
        assertEquals(500, response.getBody().getCode());
        
        // Critical: Implementation details should NOT appear
        assertFalse(response.getBody().getMessage().contains("JSON"));
        assertFalse(response.getBody().getMessage().contains("line 42"));
        assertFalse(response.getBody().getMessage().contains("Unexpected token"));
        assertFalse(response.getBody().getMessage().contains("null"));
        
        // Should return generic message with unified traceId format
        assertEquals("Internal server error. (traceId: " + extractTraceId(response) + ")",
                    response.getBody().getMessage());
    }

    @Test
    @DisplayName("MethodArgumentNotValidException: Returns validation details (safe by design)")
    void testValidationException_ReturnsValidationDetails() throws Exception {
        // Bean validation errors are safe to return (designed for clients)
        BeanPropertyBindingResult bindingResult = new BeanPropertyBindingResult(new Object(), "request");
        bindingResult.addError(new FieldError("request", "userId", "user_id is required"));
        bindingResult.addError(new FieldError("request", "itemId", "item_id is required"));
        
        MethodArgumentNotValidException ex = new MethodArgumentNotValidException(
            null, bindingResult);
        
        ResponseEntity<CommonApiResponse<String>> response = handler.handleValidationErrors(ex);
        
        assertEquals(HttpStatus.BAD_REQUEST, response.getStatusCode());
        assertEquals(400, response.getBody().getCode());
        assertTrue(response.getBody().getMessage().contains("user_id is required"));
        assertTrue(response.getBody().getMessage().contains("item_id is required"));
        assertTrue(response.getBody().getMessage().contains("traceId:"));
    }

    @Test
    @DisplayName("All error responses include a resolvable traceId")
    void testAllErrorsIncludeTraceId() {
        ResponseEntity<CommonApiResponse<String>> r1 = handler.handleBusinessValidation(
            new BusinessValidationException("test"));
        ResponseEntity<CommonApiResponse<String>> r2 = handler.handleIllegalArgument(
            new IllegalArgumentException("test"));
        ResponseEntity<CommonApiResponse<String>> r3 = handler.handleInferenceServiceException(
            new InferenceServiceException("test"));

        String traceId1 = extractTraceId(r1);
        String traceId2 = extractTraceId(r2);
        String traceId3 = extractTraceId(r3);

        assertNotNull(traceId1);
        assertNotNull(traceId2);
        assertNotNull(traceId3);

        // In test context there is no active OTel span, so all responses carry the
        // sentinel "trace-unavailable".  In production an active span produces the
        // 32-char W3C trace-id.  Either value is acceptable; a random pseudo-ID is not.
        assertTrue(isValidTraceId(traceId1), "traceId must be a W3C trace-id or 'trace-unavailable', got: " + traceId1);
        assertTrue(isValidTraceId(traceId2), "traceId must be a W3C trace-id or 'trace-unavailable', got: " + traceId2);
        assertTrue(isValidTraceId(traceId3), "traceId must be a W3C trace-id or 'trace-unavailable', got: " + traceId3);

        // Uniqueness is NOT asserted: multiple errors in the same request share one
        // trace ID by design, and in test context all carry the same sentinel value.
    }

    /** Accepts the no-span sentinel or a 32-char lowercase hex W3C trace-id. */
    private boolean isValidTraceId(String traceId) {
        if ("trace-unavailable".equals(traceId)) return true;
        return traceId != null && traceId.matches("[0-9a-f]{32}");
    }

    @Test
    @DisplayName("Error responses have correct HTTP status codes")
    void testCorrectHttpStatusCodes() {
        assertEquals(400, handler.handleBusinessValidation(
            new BusinessValidationException("test")).getStatusCode().value());
        
        assertEquals(400, handler.handleIllegalArgument(
            new IllegalArgumentException("test")).getStatusCode().value());
        
        assertEquals(503, handler.handleInferenceServiceException(
            new InferenceServiceException("test")).getStatusCode().value());
        
        assertEquals(503, handler.handleEventTrackingUnavailable(
            new EventTrackingUnavailableException("test", null)).getStatusCode().value());
        
        assertEquals(500, handler.handleRuntimeException(
            new RuntimeException("test")).getStatusCode().value());
        
        assertEquals(500, handler.handleException(
            new Exception("test")).getStatusCode().value());
    }

    /**
     * Extract the raw traceId token from a response message of the form
     * {@code "... (traceId: <value>)"}.
     *
     * The token is returned verbatim — no stripping, no truncation — so both
     * the W3C 32-char hex trace-id and the sentinel {@code "trace-unavailable"}
     * are returned correctly.
     */
    private String extractTraceId(ResponseEntity<CommonApiResponse<String>> response) {
        String message = response.getBody().getMessage();
        int start = message.indexOf("traceId: ");
        if (start < 0) return null;
        String after = message.substring(start + "traceId: ".length());
        int end = after.indexOf(')');
        return end >= 0 ? after.substring(0, end).trim() : after.trim();
    }
}
