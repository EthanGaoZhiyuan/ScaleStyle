package com.scalestyle.gateway.dto;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class RecommendationDTO {
    private String itemId;
    private String name;
    private String category;
    private String description;
    private double price;
    private String imgUrl;
    
    // Metadata for resilience monitoring
    private String source;           // "ray", "redis-cache", "popular-fallback"
    private boolean degraded;        // true if fallback was used
    private String degradedReason;   // Exception type that triggered fallback
    
    // Week 3: AI recommendation reason (Top-1 only)
    private String reason;           // AI-generated reason (1 sentence), empty if fallback or not Top-1
    private String reasonSource;     // "llm", "template", or "fallback"
}
