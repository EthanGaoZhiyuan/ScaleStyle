package com.scalestyle.gateway.dto;

import jakarta.validation.ConstraintViolation;
import jakarta.validation.Validation;
import jakarta.validation.Validator;
import jakarta.validation.ValidatorFactory;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for Bean Validation on request DTOs.
 * Tests controller-layer schema validation (not service-layer business logic).
 */
class RequestValidationTest {

    private static Validator validator;

    @BeforeAll
    static void setup() {
        ValidatorFactory factory = Validation.buildDefaultValidatorFactory();
        validator = factory.getValidator();
    }

    // ===================================
    // TrackClickRequest Validation Tests
    // ===================================

    @Test
    @DisplayName("TrackClickRequest: Valid request should pass")
    void testTrackClickRequest_Valid() {
        TrackClickRequest request = TrackClickRequest.builder()
                .userId("user-123")
                .itemId("item-456")
                .sessionId("session-789")
                .source("search")
                .query("red dress")
                .position(0)
                .device("mobile")
                .build();

        Set<ConstraintViolation<TrackClickRequest>> violations = validator.validate(request);
        assertTrue(violations.isEmpty(), "Valid request should have no violations");
    }

    @Test
    @DisplayName("TrackClickRequest: Missing userId should fail")
    void testTrackClickRequest_MissingUserId() {
        TrackClickRequest request = TrackClickRequest.builder()
                .itemId("item-456")
                .sessionId("session-789")
                .source("search")
                .build();

        Set<ConstraintViolation<TrackClickRequest>> violations = validator.validate(request);
        assertTrue(violations.stream().anyMatch(v -> v.getMessage().contains("user_id is required")));
    }

    @Test
    @DisplayName("TrackClickRequest: Missing itemId should fail")
    void testTrackClickRequest_MissingItemId() {
        TrackClickRequest request = TrackClickRequest.builder()
                .userId("user-123")
                .sessionId("session-789")
                .source("search")
                .build();

        Set<ConstraintViolation<TrackClickRequest>> violations = validator.validate(request);
        assertTrue(violations.stream().anyMatch(v -> v.getMessage().contains("item_id is required")));
    }

    @Test
    @DisplayName("TrackClickRequest: Too long userId should fail")
    void testTrackClickRequest_UserIdTooLong() {
        String longUserId = "x".repeat(101);
        TrackClickRequest request = TrackClickRequest.builder()
                .userId(longUserId)
                .itemId("item-456")
                .sessionId("session-789")
                .source("search")
                .build();

        Set<ConstraintViolation<TrackClickRequest>> violations = validator.validate(request);
        assertTrue(violations.stream().anyMatch(v -> v.getMessage().contains("must not exceed 100 characters")));
    }

    @Test
    @DisplayName("TrackClickRequest: image_search source should pass")
    void testTrackClickRequest_ImageSearchSource() {
        TrackClickRequest request = TrackClickRequest.builder()
                .userId("user-123")
                .itemId("item-456")
                .sessionId("session-789")
                .source("image_search")
                .build();

        Set<ConstraintViolation<TrackClickRequest>> violations = validator.validate(request);
        assertTrue(violations.isEmpty(), "image_search is a valid source and should pass DTO validation");
    }

    @Test
    @DisplayName("TrackClickRequest: unknown source should fail")
    void testTrackClickRequest_UnknownSourceRejected() {
        TrackClickRequest request = TrackClickRequest.builder()
                .userId("user-123")
                .itemId("item-456")
                .sessionId("session-789")
                .source("unknown")
                .build();

        Set<ConstraintViolation<TrackClickRequest>> violations = validator.validate(request);
        assertTrue(violations.stream().anyMatch(v -> v.getMessage().contains("must be one of")));
    }

    @Test
    @DisplayName("TrackClickRequest: Invalid source should fail")
    void testTrackClickRequest_InvalidSource() {
        TrackClickRequest request = TrackClickRequest.builder()
                .userId("user-123")
                .itemId("item-456")
                .sessionId("session-789")
                .source("invalid-source")
                .build();

        Set<ConstraintViolation<TrackClickRequest>> violations = validator.validate(request);
        assertTrue(violations.stream().anyMatch(v -> v.getMessage().contains("must be one of")));
    }

    @Test
    @DisplayName("TrackClickRequest: Negative position should fail")
    void testTrackClickRequest_NegativePosition() {
        TrackClickRequest request = TrackClickRequest.builder()
                .userId("user-123")
                .itemId("item-456")
                .sessionId("session-789")
                .source("search")
                .position(-1)
                .build();

        Set<ConstraintViolation<TrackClickRequest>> violations = validator.validate(request);
        assertTrue(violations.stream().anyMatch(v -> v.getMessage().contains("must be non-negative")));
    }

    @Test
    @DisplayName("TrackClickRequest: Too long query should fail")
    void testTrackClickRequest_QueryTooLong() {
        String longQuery = "x".repeat(501);
        TrackClickRequest request = TrackClickRequest.builder()
                .userId("user-123")
                .itemId("item-456")
                .sessionId("session-789")
                .source("search")
                .query(longQuery)
                .build();

        Set<ConstraintViolation<TrackClickRequest>> violations = validator.validate(request);
        assertTrue(violations.stream().anyMatch(v -> v.getMessage().contains("must not exceed 500 characters")));
    }

    // ===================================
    // ImageSearchRequest Validation Tests
    // ===================================

    @Test
    @DisplayName("ImageSearchRequest: Valid text_to_image request should pass")
    void testImageSearchRequest_ValidTextToImage() {
        ImageSearchRequest request = ImageSearchRequest.builder()
                .mode("text_to_image")
                .query("red dress")
                .k(10)
                .userId("user-123")
                .debug(false)
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertTrue(violations.isEmpty(), "Valid request should have no violations");
    }

    @Test
    @DisplayName("ImageSearchRequest: Valid image_to_image request should pass")
    void testImageSearchRequest_ValidImageToImage() {
        ImageSearchRequest request = ImageSearchRequest.builder()
                .mode("image_to_image")
                .imageHash("abc123")
                .k(20)
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertTrue(violations.isEmpty(), "Valid request should have no violations");
    }

    @Test
    @DisplayName("ImageSearchRequest: Valid multimodal request should pass")
    void testImageSearchRequest_ValidMultimodal() {
        ImageSearchRequest request = ImageSearchRequest.builder()
                .mode("multimodal")
                .query("red dress")
                .imageUrl("https://example.com/reference.jpg")
                .k(10)
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertTrue(violations.isEmpty(), "Valid request should have no violations");
    }

    @Test
    @DisplayName("ImageSearchRequest: Missing mode should fail")
    void testImageSearchRequest_MissingMode() {
        ImageSearchRequest request = ImageSearchRequest.builder()
                .query("red dress")
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertEquals(1, violations.size());
        assertTrue(violations.iterator().next().getMessage().contains("mode is required"));
    }

    @Test
    @DisplayName("ImageSearchRequest: Invalid mode should fail")
    void testImageSearchRequest_InvalidMode() {
        ImageSearchRequest request = ImageSearchRequest.builder()
                .mode("invalid_mode")
                .query("red dress")
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertEquals(1, violations.size());
        assertTrue(violations.iterator().next().getMessage().contains("must be one of"));
    }

    @Test
    @DisplayName("ImageSearchRequest: k too small should fail")
    void testImageSearchRequest_KTooSmall() {
        ImageSearchRequest request = ImageSearchRequest.builder()
                .mode("text_to_image")
                .query("red dress")
                .k(0)
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertEquals(1, violations.size());
        assertTrue(violations.iterator().next().getMessage().contains("must be at least 1"));
    }

    @Test
    @DisplayName("ImageSearchRequest: k too large should fail")
    void testImageSearchRequest_KTooLarge() {
        ImageSearchRequest request = ImageSearchRequest.builder()
                .mode("text_to_image")
                .query("red dress")
                .k(101)
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertEquals(1, violations.size());
        assertTrue(violations.iterator().next().getMessage().contains("must not exceed 100"));
    }

    @Test
    @DisplayName("ImageSearchRequest: Too long query should fail")
    void testImageSearchRequest_QueryTooLong() {
        String longQuery = "x".repeat(501);
        ImageSearchRequest request = ImageSearchRequest.builder()
                .mode("text_to_image")
                .query(longQuery)
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertEquals(1, violations.size());
        assertTrue(violations.iterator().next().getMessage().contains("must not exceed 500 characters"));
    }

    @Test
    @DisplayName("ImageSearchRequest: Too long image URL should fail")
    void testImageSearchRequest_ImageUrlTooLong() {
        String longUrl = "https://example.com/" + "x".repeat(2030);
        ImageSearchRequest request = ImageSearchRequest.builder()
                .mode("image_to_image")
                .imageUrl(longUrl)
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertEquals(1, violations.size());
        assertTrue(violations.iterator().next().getMessage().contains("must not exceed 2048 characters"));
    }

    @Test
    @DisplayName("ImageSearchRequest: Too long userId should fail")
    void testImageSearchRequest_UserIdTooLong() {
        String longUserId = "x".repeat(101);
        ImageSearchRequest request = ImageSearchRequest.builder()
                .mode("text_to_image")
                .query("red dress")
                .userId(longUserId)
                .build();

        Set<ConstraintViolation<ImageSearchRequest>> violations = validator.validate(request);
        assertEquals(1, violations.size());
        assertTrue(violations.iterator().next().getMessage().contains("must not exceed 100 characters"));
    }
}
