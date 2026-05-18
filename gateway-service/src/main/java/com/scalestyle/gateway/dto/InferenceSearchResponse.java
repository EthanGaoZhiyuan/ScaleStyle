package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.JsonToken;
import com.fasterxml.jackson.databind.DeserializationContext;
import com.fasterxml.jackson.databind.annotation.JsonDeserialize;
import com.fasterxml.jackson.databind.deser.std.StdDeserializer;
import lombok.Data;

import java.io.IOException;
import java.util.List;
import java.util.Map;

@Data
public class InferenceSearchResponse {
    private String query;
    private Map<String, Object> route;
    private List<ResultItem> results;

    @JsonProperty("request_id")
    private String request_id;

    @JsonProperty("latency_ms")
    private Map<String, Object> latency_ms;

    private boolean degraded;

    @JsonProperty("degraded_reason")
    private String degradedReason;

    private String source;

    @Data
    public static class ResultItem {

        @JsonProperty("article_id")
        @JsonDeserialize(using = InferenceSearchResponse.ArticleIdDeserializer.class)
        private String article_id;

        private Double score;
        private Map<String, Object> meta;

        // Week 3: AI recommendation reason
        private String reason;

        @JsonProperty("reason_source")
        private String reasonSource;
    }

    /**
     * Deserializes {@code article_id} to a canonical 10-digit zero-padded {@link String}.
     *
     * <p>Handles two JSON representations the inference service may emit:
     * <ul>
     *   <li>JSON integer (e.g. {@code 127563002}) — produced if Milvus returns an Int64
     *       primary key before {@code contract_normalize} zero-pads it.</li>
     *   <li>JSON string (e.g. {@code "0127563002"} or {@code "127563002"}) — the standard
     *       output of the inference {@code contract_normalize} step, which calls
     *       {@code str(aid).zfill(10)}.</li>
     * </ul>
     *
     * <p>Any purely numeric value shorter than 10 digits is left-padded with zeros to match
     * the canonical H&M article ID format. Non-numeric strings (e.g. test fixture IDs
     * like {@code "item1"}) are returned unchanged.
     */
    static class ArticleIdDeserializer extends StdDeserializer<String> {

        ArticleIdDeserializer() {
            super(String.class);
        }

        @Override
        public String deserialize(JsonParser p, DeserializationContext ctx) throws IOException {
            String raw;
            if (p.currentToken() == JsonToken.VALUE_NUMBER_INT) {
                raw = String.valueOf(p.getLongValue());
            } else {
                raw = p.getText();
            }
            return canonicalize(raw);
        }

        static String canonicalize(String raw) {
            if (raw == null || raw.isEmpty()) return raw;
            if (raw.chars().allMatch(Character::isDigit) && raw.length() < 10) {
                return "0".repeat(10 - raw.length()) + raw;
            }
            return raw;
        }
    }
}
