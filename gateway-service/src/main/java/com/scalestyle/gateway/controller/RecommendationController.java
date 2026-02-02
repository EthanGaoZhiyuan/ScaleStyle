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

    @GetMapping("/debug/cache-stats")
    public Map<String, Object> cacheStats() {
        return recommendationService.getCacheStats();
    }

    @GetMapping("/search")
    public CommonApiResponse<List<RecommendationDTO>> search(
            @RequestParam String query,
            @RequestParam(required = false) String userId,
            @RequestParam(required = false, defaultValue = "search") String intent,
            @RequestParam(defaultValue = "10") Integer k,
            @RequestParam(defaultValue = "false") Boolean debug
    ) {
        return CommonApiResponse.success(recommendationService.search(query, userId, intent, k, debug));
    }

}
