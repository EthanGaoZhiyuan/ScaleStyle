package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import lombok.Builder;
import lombok.Data;

@Data
@Builder
public class InferenceSearchRequest {
    private String query;
    @Builder.Default
    private Integer k = 10;
    @Builder.Default
    private Boolean debug = false;
    @JsonProperty("user_id")
    private String userId;
    // P0-1+: Add intent for multi-intent support (search/similar/outfit/trend)
    private String intent;
}
