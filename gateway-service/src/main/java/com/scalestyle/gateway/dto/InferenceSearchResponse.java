package com.scalestyle.gateway.dto;

import lombok.Data;

import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.annotation.JsonProperty;

@Data
public class InferenceSearchResponse {
    private String query;
    private Map<String, Object> route;
    private List<ResultItem> results;

    @JsonProperty("request_id")
    private String request_id;

    @JsonProperty("latency_ms")
    private Map<String, Object> latency_ms;

    @Data
    public static class ResultItem {

        @JsonProperty("article_id")
        private Object article_id;
        
        private Double score;
        private Map<String, Object> meta;
    }
}
