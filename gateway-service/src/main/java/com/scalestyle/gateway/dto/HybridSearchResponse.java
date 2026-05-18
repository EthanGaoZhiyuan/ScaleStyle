package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
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
@JsonIgnoreProperties(ignoreUnknown = true)
@Schema(description = "Hybrid text + image search response")
public class HybridSearchResponse {

    @Schema(description = "List of hybrid search results with per-source scores")
    private List<HybridItemDTO> items;

    @Schema(description = "Number of results returned", example = "10")
    private Integer k;

    @Schema(description = "Search mode", example = "hybrid")
    private String mode;

    @Schema(description = "Pipeline architecture", example = "dual_recall_normalized_fusion")
    private String architecture;

    @Schema(description = "Status of the search", example = "success")
    private String status;

    @Schema(description = "Whether the response was degraded due to a partial failure")
    private Boolean degraded;

    @JsonProperty("degraded_reason")
    @Schema(description = "Stable degradation reason vocabulary if applicable")
    private String degradedReason;

    @JsonProperty("request_id")
    @Schema(description = "Request ID for tracing")
    private String requestId;

    @JsonProperty("latency_ms")
    @Schema(description = "Total latency in milliseconds")
    private Double latencyMs;
}
