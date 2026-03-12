package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import io.swagger.v3.oas.annotations.media.Schema;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import jakarta.validation.constraints.Min;

/**
 * HTTP transport model for /events/click requests.
 * This object must remain request-focused and should not be reused as Kafka payload.
 * 
 * Validation Strategy:
 * - Controller layer: Basic schema/field format validation (this class)
 * - Service layer: Business/domain invariant validation
 */
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
@Schema(description = "Track click request payload")
public class TrackClickRequest {

    @NotBlank(message = "user_id is required")
    @Size(max = 100, message = "user_id must not exceed 100 characters")
    @JsonProperty("user_id")
    private String userId;

    @NotBlank(message = "item_id is required")
    @Size(max = 100, message = "item_id must not exceed 100 characters")
    @JsonProperty("item_id")
    private String itemId;

    @NotBlank(message = "session_id is required")
    @Size(max = 100, message = "session_id must not exceed 100 characters")
    @JsonProperty("session_id")
    private String sessionId;

    @NotBlank(message = "source is required")
    @Pattern(regexp = EventSource.ALLOWED_PATTERN, message = "source must be one of: search, browse, recommendation, image_search")
    @JsonProperty("source")
    private String source;

    @Size(max = 500, message = "query must not exceed 500 characters")
    @JsonProperty("query")
    private String query;

    @Size(max = 100, message = "image_hash must not exceed 100 characters")
    @JsonProperty("image_hash")
    private String imageHash;

    @Min(value = 0, message = "position must be non-negative")
    @JsonProperty("position")
    private Integer position;

    @Size(max = 50, message = "device must not exceed 50 characters")
    @JsonProperty("device")
    private String device;
}
