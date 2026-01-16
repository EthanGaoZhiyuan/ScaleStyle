package com.scalestyle.gateway.controller;

import com.scalestyle.gateway.dto.RecommendationDTO;
import com.scalestyle.gateway.exception.CommonApiResponse;
import com.scalestyle.gateway.service.RecommendationService;
import io.swagger.v3.oas.annotations.tags.Tag;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/recommendation")
@RequiredArgsConstructor
@Tag(name = "Recommendation", description = "Core recommendation endpoints")
public class RecommendationController {
    private final RecommendationService recommendationService;

    @GetMapping("/debug/cache-check")
    public Map<String, Object> debugCache(@RequestParam(required = false) String id) {
        // If ID is provided, query this one; otherwise return cache size and first 10 entries
        if (id != null) {
            return Map.of(
                    "queryId", id,
                    "exists", recommendationService.getProductCache().containsKey(id),
                    "data", recommendationService.getProductCache().get(id)
            );
        }
        return Map.of(
                "cacheSize", recommendationService.getProductCache().size(),
                "sampleKeys", recommendationService.getProductCache().keySet().stream().limit(10).toList()
        );
    }

    @GetMapping("/search")
    public CommonApiResponse<List<RecommendationDTO>> search(
            @RequestParam String query,
            @RequestParam(required = false) String userId,
            @RequestParam(defaultValue = "10") Integer k,
            @RequestParam(defaultValue = "false") Boolean debug
    ) {
        return CommonApiResponse.success(recommendationService.search(query, userId, k, debug));
    }

}
