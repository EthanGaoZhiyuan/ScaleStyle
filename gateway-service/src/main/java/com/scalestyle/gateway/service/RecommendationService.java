package com.scalestyle.gateway.service;

import com.scalestyle.gateway.dto.InferenceSearchRequest;
import com.scalestyle.gateway.dto.InferenceSearchResponse;
import com.scalestyle.gateway.dto.RecommendationDTO;
import com.scalestyle.gateway.exception.InferenceServiceException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.Getter;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

import jakarta.annotation.PostConstruct;
import java.io.File;
import java.io.IOException;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

@Slf4j
@Service
public class RecommendationService {

    private final RestClient restClient;

    public RecommendationService(RestClient inferenceRestClient) {
        this.restClient = inferenceRestClient;
    }

    @Getter
    private Map<String, RecommendationDTO> productCache;

    @Value("${METADATA_PATH:/app/data/product_metadata.json}")
    private String metadataPath;

    private final ObjectMapper objectMapper = new ObjectMapper();

    @PostConstruct
    public void init() {
        log.info("Initializing Product Cache from: {}", metadataPath);
        try {
            File file = new File(metadataPath);
            if (file.exists()) {
                productCache = objectMapper.readValue(file, new TypeReference<Map<String, RecommendationDTO>>() {});
                log.info("Successfully loaded {} products into memory.", productCache.size());
            } else {
                log.warn("Metadata file not found at {}. Using empty cache.", metadataPath);
                productCache = Collections.emptyMap();
            }
        } catch (IOException e) {
            log.error("Failed to load metadata JSON", e);
            productCache = Collections.emptyMap();
        }
    }

    public List<RecommendationDTO> search(String query, String userId, int k, boolean debug) {
        InferenceSearchRequest req = InferenceSearchRequest.builder()
            .query(query)
            .k(k)
            .debug(debug)
            .userId(userId)
            .build();

        final InferenceSearchResponse resp;
        try {
            resp = restClient.post()
                    .uri("/search")
                    .body(req)
                    .retrieve()
                    .body(InferenceSearchResponse.class);
        } catch (Exception e) {
            throw new InferenceServiceException("Inference service call failed", e);
        }

        if (resp == null || resp.getResults() == null) {
            throw new InferenceServiceException("Inference service returned empty response");
        }

        return resp.getResults().stream()
                .map(item -> {
                    String rawId = String.valueOf(item.getArticle_id());
                    String lookupId = rawId.length() < 10 ? "0".repeat(10 - rawId.length()) + rawId : rawId;

                    return productCache.getOrDefault(lookupId, RecommendationDTO.builder()
                            .itemId(rawId)
                            .name("Unknown Product")
                            .category("General")
                            .description("Description unavailable")
                            .price(0.00)
                            .imgUrl("https://via.placeholder.com/150")
                            .build());
                })
                .collect(Collectors.toList());
    }
}
