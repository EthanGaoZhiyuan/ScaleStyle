package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import io.swagger.v3.oas.annotations.media.Schema;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.util.List;

/**
 * Per-item result for hybrid text+image search.
 * Extends the standard recommendation fields with per-source scores
 * from the dual-recall normalized fusion pipeline.
 */
@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
@JsonIgnoreProperties(ignoreUnknown = true)
@Schema(description = "Hybrid search result item with per-source scores")
public class HybridItemDTO {

    @Schema(description = "Canonical 10-digit article ID", example = "0108775015")
    private String itemId;

    @Schema(description = "Product name")
    private String name;

    @Schema(description = "Product category")
    private String category;

    @Schema(description = "Product description")
    private String description;

    @Schema(description = "Normalized price")
    private double price;

    @Schema(description = "Product image URL")
    private String imgUrl;

    @Schema(description = "Result source", example = "hybrid")
    private String source;

    @Schema(description = "Whether result is degraded")
    private boolean degraded;

    @Schema(description = "Degradation reason if applicable")
    private String degradedReason;

    @Schema(description = "Recommendation reason")
    private String reason;

    @Schema(description = "Reason source")
    private String reasonSource;

    // Hybrid-specific score fields

    @JsonProperty("finalScore")
    @Schema(description = "Weighted normalized fusion score", example = "0.72")
    private Double finalScore;

    @JsonProperty("imageScore")
    @Schema(description = "Raw CLIP image similarity score", example = "0.91")
    private Double imageScore;

    @JsonProperty("textScore")
    @Schema(description = "Raw BGE-small text similarity score", example = "0.68")
    private Double textScore;

    @JsonProperty("behaviorScore")
    @Schema(description = "Behavior boost score (0.0 until Phase 7)", example = "0.0")
    private Double behaviorScore;

    @JsonProperty("candidateSources")
    @Schema(description = "Which recall branches contributed this candidate", example = "[\"image\", \"text\"]")
    private List<String> candidateSources;
}
