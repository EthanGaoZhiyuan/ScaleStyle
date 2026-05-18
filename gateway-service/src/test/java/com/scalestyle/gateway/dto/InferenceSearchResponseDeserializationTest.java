package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Verifies that {@link InferenceSearchResponse.ResultItem#getArticle_id()} is
 * always a canonical 10-digit zero-padded String regardless of how the inference
 * service serializes the article_id field.
 *
 * <p>Two JSON representations are supported:
 * <ul>
 *   <li>JSON integer — Milvus Int64 primary key before contract_normalize runs.</li>
 *   <li>JSON string — standard output of the inference contract_normalize step
 *       ({@code str(aid).zfill(10)}).</li>
 * </ul>
 */
class InferenceSearchResponseDeserializationTest {

    private final ObjectMapper mapper = new ObjectMapper();

    // ── ArticleIdDeserializer.canonicalize() unit tests ───────────────────────

    @Test
    @DisplayName("canonicalize: 9-digit numeric string → left-padded to 10 digits")
    void canonicalize_nineDigitString_padded() {
        assertThat(InferenceSearchResponse.ArticleIdDeserializer.canonicalize("127563002"))
                .isEqualTo("0127563002");
    }

    @Test
    @DisplayName("canonicalize: already 10-digit string → unchanged")
    void canonicalize_tenDigitString_unchanged() {
        assertThat(InferenceSearchResponse.ArticleIdDeserializer.canonicalize("0127563002"))
                .isEqualTo("0127563002");
    }

    @Test
    @DisplayName("canonicalize: 7-digit numeric string → padded with three leading zeros")
    void canonicalize_sevenDigitString_padded() {
        assertThat(InferenceSearchResponse.ArticleIdDeserializer.canonicalize("1234567"))
                .isEqualTo("0001234567");
    }

    @Test
    @DisplayName("canonicalize: non-numeric string (test fixture ID) → returned unchanged")
    void canonicalize_nonNumericString_unchanged() {
        assertThat(InferenceSearchResponse.ArticleIdDeserializer.canonicalize("item1"))
                .isEqualTo("item1");
    }

    @Test
    @DisplayName("canonicalize: null → null")
    void canonicalize_null_returnsNull() {
        assertThat(InferenceSearchResponse.ArticleIdDeserializer.canonicalize(null))
                .isNull();
    }

    @Test
    @DisplayName("canonicalize: empty string → empty string")
    void canonicalize_empty_returnsEmpty() {
        assertThat(InferenceSearchResponse.ArticleIdDeserializer.canonicalize(""))
                .isEmpty();
    }

    // ── JSON deserialization via ObjectMapper ─────────────────────────────────

    @Test
    @DisplayName("JSON integer article_id (9-digit) → canonical 10-digit string")
    void deserialize_jsonInteger_9digit_padded() throws Exception {
        String json = """
                {"results": [{"article_id": 127563002}]}
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        assertThat(response.getResults()).hasSize(1);
        assertThat(response.getResults().get(0).getArticle_id()).isEqualTo("0127563002");
    }

    @Test
    @DisplayName("JSON integer article_id (10-digit) → unchanged 10-digit string")
    void deserialize_jsonInteger_10digit_unchanged() throws Exception {
        String json = """
                {"results": [{"article_id": 9999999999}]}
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        assertThat(response.getResults().get(0).getArticle_id()).isEqualTo("9999999999");
    }

    @Test
    @DisplayName("JSON string article_id already zero-padded → unchanged")
    void deserialize_jsonString_alreadyPadded_unchanged() throws Exception {
        String json = """
                {"results": [{"article_id": "0127563002"}]}
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        assertThat(response.getResults().get(0).getArticle_id()).isEqualTo("0127563002");
    }

    @Test
    @DisplayName("JSON string article_id missing leading zero → padded")
    void deserialize_jsonString_missingLeadingZero_padded() throws Exception {
        String json = """
                {"results": [{"article_id": "127563002"}]}
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        assertThat(response.getResults().get(0).getArticle_id()).isEqualTo("0127563002");
    }

    @Test
    @DisplayName("JSON string non-numeric article_id (test fixture) → unchanged")
    void deserialize_jsonString_nonNumeric_unchanged() throws Exception {
        String json = """
                {"results": [{"article_id": "item1"}]}
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        assertThat(response.getResults().get(0).getArticle_id()).isEqualTo("item1");
    }

    @Test
    @DisplayName("Multiple results all get canonical article_id")
    void deserialize_multipleResults_allCanonicalized() throws Exception {
        String json = """
                {"results": [
                    {"article_id": 127563002},
                    {"article_id": "0856000020"},
                    {"article_id": "856000020"}
                ]}
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        assertThat(response.getResults())
                .extracting(InferenceSearchResponse.ResultItem::getArticle_id)
                .containsExactly("0127563002", "0856000020", "0856000020");
    }

    @Test
    @DisplayName("Full inference response with auxiliary fields deserializes correctly")
    void deserialize_fullResultItem_allFieldsPresent() throws Exception {
        String json = """
                {
                  "query": "black dress",
                  "results": [{
                    "article_id": 827955002,
                    "score": 0.92,
                    "reason": "Matches your style",
                    "reason_source": "llm"
                  }]
                }
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        InferenceSearchResponse.ResultItem item = response.getResults().get(0);
        assertThat(item.getArticle_id()).isEqualTo("0827955002");
        assertThat(item.getScore()).isEqualTo(0.92);
        assertThat(item.getReason()).isEqualTo("Matches your style");
        assertThat(item.getReasonSource()).isEqualTo("llm");
    }

    // ── M-5: degraded_reason snake_case binding ───────────────────────────────

    @Test
    @DisplayName("degraded_reason (snake_case) binds to degradedReason field")
    void deserialize_degradedResponse_degradedReasonMapped() throws Exception {
        String json = """
                {
                  "results": [{"article_id": "0108775015", "score": 0.95}],
                  "degraded": true,
                  "degraded_reason": "INFERENCE_TIMEOUT",
                  "source": "popular-fallback"
                }
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        assertThat(response.isDegraded()).isTrue();
        assertThat(response.getDegradedReason()).isEqualTo("INFERENCE_TIMEOUT");
        assertThat(response.getSource()).isEqualTo("popular-fallback");
    }

    @Test
    @DisplayName("Non-degraded inference response has degraded=false and null degradedReason")
    void deserialize_successResponse_notDegraded() throws Exception {
        String json = """
                {
                  "results": [{"article_id": "0108775015", "score": 0.95}],
                  "degraded": false
                }
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        assertThat(response.isDegraded()).isFalse();
        assertThat(response.getDegradedReason()).isNull();
    }

    @Test
    @DisplayName("Missing degraded field defaults to false (not present in older inference payloads)")
    void deserialize_missingDegradedField_defaultsFalse() throws Exception {
        String json = """
                {"results": [{"article_id": "0108775015", "score": 0.95}]}
                """;
        InferenceSearchResponse response = mapper.readValue(json, InferenceSearchResponse.class);
        assertThat(response.isDegraded()).isFalse();
        assertThat(response.getDegradedReason()).isNull();
    }
}
