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
import jakarta.validation.constraints.Max;

/**
 * Image search request DTO with input validation.
 * 
 * Validation Strategy:
 * - Controller layer: Basic schema/field format validation (this class)
 * - Service layer: Business logic validation (mode-specific requirements)
 */
@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
@Schema(description = "Image search request")
public class ImageSearchRequest {
    
    @NotBlank(message = "mode is required")
    @Pattern(regexp = "text_to_image|image_to_image|multimodal", message = "mode must be one of 'text_to_image', 'image_to_image', or 'multimodal'")
    @Schema(description = "Search mode: text_to_image, image_to_image, or multimodal", example = "text_to_image")
    private String mode;
    
    @Size(max = 500, message = "query must not exceed 500 characters")
    @Schema(description = "Text query for text-to-image search", example = "red dress")
    private String query;
    
    @Size(max = 2048, message = "image_url must not exceed 2048 characters")
    @Schema(description = "Image URL for image-to-image search")
    @JsonProperty("image_url")
    private String imageUrl;
    
    @Size(max = 100, message = "image_hash must not exceed 100 characters")
    @Schema(description = "Image hash for image-to-image search")
    @JsonProperty("image_hash")
    private String imageHash;
    
    @Min(value = 1, message = "k must be at least 1")
    @Max(value = 100, message = "k must not exceed 100")
    @Schema(description = "Number of results to return", example = "10")
    @Builder.Default
    private Integer k = 10;
    
    @Size(max = 100, message = "user_id must not exceed 100 characters")
    @Schema(description = "User ID for personalization")
    @JsonProperty("user_id")
    private String userId;
    
    @Schema(description = "Enable debug mode", example = "false")
    @Builder.Default
    private Boolean debug = false;
}
