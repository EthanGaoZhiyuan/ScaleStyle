package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
@Schema(description = "Hybrid text + image search request")
public class HybridSearchRequest {

    @NotBlank(message = "query is required")
    @Size(max = 500, message = "query must not exceed 500 characters")
    @Schema(description = "Text query", example = "similar but black")
    private String query;

    @NotBlank(message = "image_base64 is required")
    @Size(max = 5_000_000, message = "image_base64 must not exceed 5 MB encoded")
    @JsonProperty("image_base64")
    @Schema(description = "Base64-encoded image bytes")
    private String imageBase64;

    @Min(value = 1, message = "k must be at least 1")
    @Max(value = 100, message = "k must not exceed 100")
    @Schema(description = "Number of results to return", example = "10")
    @Builder.Default
    private Integer k = 10;

    @Size(max = 100, message = "userId must not exceed 100 characters")
    @JsonProperty("userId")
    @Schema(description = "User ID for future personalization")
    private String userId;

    @JsonProperty("image_weight")
    @Schema(description = "Weight for image recall scores (default 0.5)", example = "0.5")
    @Builder.Default
    private Double imageWeight = 0.5;

    @JsonProperty("text_weight")
    @Schema(description = "Weight for text recall scores (default 0.4)", example = "0.4")
    @Builder.Default
    private Double textWeight = 0.4;

    @JsonProperty("behavior_weight")
    @Schema(description = "Weight for behavior boost (default 0.1; behavior_score=0 until Phase 7)", example = "0.1")
    @Builder.Default
    private Double behaviorWeight = 0.1;

    @Schema(description = "Enable debug mode", example = "false")
    @Builder.Default
    private Boolean debug = false;
}
