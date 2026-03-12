package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import io.swagger.v3.oas.annotations.media.Schema;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
@Schema(description = "Image search response")
public class ImageSearchResponse {
    
    @Schema(description = "List of recommended items")
    private List<RecommendationDTO> items;
    
    @Schema(description = "Number of results returned", example = "10")
    private Integer k;
    
    @Schema(description = "Search mode used", example = "text_to_image")
    private String mode;
    
    @Schema(description = "Status of the search", example = "success")
    private String status;
    
    @Schema(description = "Whether the response was degraded due to fallback")
    private Boolean degraded;
    
    @Schema(description = "Stable degradation reason vocabulary if applicable")
    @JsonProperty("degraded_reason")
    private String degradedReason;
    
    @Schema(description = "Request ID for tracking")
    @JsonProperty("request_id")
    private String requestId;
    
    @Schema(description = "Total latency in milliseconds")
    @JsonProperty("latency_ms")
    private Double latencyMs;
}
