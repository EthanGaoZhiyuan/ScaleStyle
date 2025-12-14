package com.scalestyle.gateway.service;

import com.example.gateway.grpc.RecommendationResponse;
import com.example.gateway.grpc.RecommendationServiceGrpc;
import com.example.gateway.grpc.UserRequest;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.scalestyle.gateway.dto.RecommendationDTO;
import lombok.Getter;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import jakarta.annotation.PostConstruct;
import java.io.File;
import java.io.IOException;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import net.devh.boot.grpc.client.inject.GrpcClient;

@Slf4j
@Service
public class RecommendationService {

    @GrpcClient("inference-service")
    private RecommendationServiceGrpc.RecommendationServiceBlockingStub stub;

    @Getter
    private Map<String, RecommendationDTO> productCache;

    @Value("${METADATA_PATH:/app/data/product_metadata.json}")
    private String metadataPath;

    private final ObjectMapper objectMapper = new ObjectMapper();

    /**
     * Initialize gRPC client connection when the application starts.
     */
    @PostConstruct
    public void init() {
        log.info("Initializing Product Cache from: {}", metadataPath);
        try {
            File file = new File(metadataPath);
            if (file.exists()) {
                // Read JSON as Map<String, RecommendationDTO>
                productCache = objectMapper.readValue(file, new TypeReference<Map<String, RecommendationDTO>>() {});
                log.info("✅ Successfully loaded {} products into memory.", productCache.size());
            } else {
                log.warn("⚠️ Metadata file not found at {}. Using empty cache.", metadataPath);
                productCache = Collections.emptyMap();
            }
        } catch (IOException e) {
            log.error("❌ Failed to load metadata JSON", e);
            productCache = Collections.emptyMap();
        }
    }

    /**
     * Send a recommendation request to the Python Server (Ray Serve)
     *
     * @param userId
     * @param k
     * @return
     */
    public List<RecommendationDTO> getRecommendations(String userId, int k) {
        // 1. Build the request object defined in the .proto file
        UserRequest request = UserRequest.newBuilder().setUserId(userId).setK(k).build();

        try {
            // 2. Execute the remote procedure call (RPC)
            RecommendationResponse response = stub.getRecommendations(request);
            return response.getItemsList().stream()
                    .map(item -> {
                        String rawId = item.getItemId();

                        // Ensure the ID is zero-padded to 10 characters
                        String lookupId = rawId.length() < 10 ? "0".repeat(10 - rawId.length()) + rawId : rawId;

                        // Log a warning if the product is not found in the cache
                        if (!productCache.containsKey(lookupId)) {
                            log.warn("⚠️ Lookup failed for ID: '{}' (Raw: '{}')", lookupId, rawId);
                        }

                        // Return the product details from cache or a default placeholder
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
        } catch (Exception e) {
            log.error("gRPC call failed for userId: {}", userId, e);
            throw new RuntimeException("RPC call to the inference service failed", e);
        }
    }
}
