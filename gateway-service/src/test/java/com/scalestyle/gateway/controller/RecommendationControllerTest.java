package com.scalestyle.gateway.controller;

import com.scalestyle.gateway.common.ResultCode;
import com.scalestyle.gateway.dto.RecommendationDTO;
import com.scalestyle.gateway.service.RecommendationService;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import java.util.List;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultHandlers.print;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;

@WebMvcTest(RecommendationController.class)
public class RecommendationControllerTest {
    @Autowired
    private MockMvc mockMvc;
    @Autowired
    private RecommendationService recommendationService;

    @MockitoBean
    private RecommendationService grpcClientService;

    @Test
    @DisplayName("Test Success: should return 200 OK")
    void testGetRecommendationsSuccess() throws Exception {
        String mockUserId = "u123";

        // Construct a mock product detail object (this is what the Service should return now)
        RecommendationDTO mockDetail = RecommendationDTO.builder()
                .itemId("706016001")
                .name("Test Black Jeans")
                .category("Trousers")
                .price(29.99)
                .imgUrl("http://mock.url/img.jpg")
                .build();

        // 2. Mock Service behavior
        // When Controller calls recommendationService.getRecommendations, return our expected List
        when(recommendationService.getRecommendations(eq(mockUserId), anyInt()))
                .thenReturn(List.of(mockDetail));

        // 3. Execute request & verify results
        mockMvc.perform(get("/api/recommendation/recommend")
                        .param("userId", mockUserId)
                        .param("k", "5"))
                .andDo(print()) // Print response log for debugging
                .andExpect(status().isOk())
                // Verify outer status code
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.message").value("Success"))

                // Verify first item in the data array (note: data is now directly an array)
                .andExpect(jsonPath("$.data[0].itemId").value("706016001"))
                .andExpect(jsonPath("$.data[0].name").value("Test Black Jeans"))
                .andExpect(jsonPath("$.data[0].price").value(29.99))
                .andExpect(jsonPath("$.data[0].imgUrl").value("http://mock.url/img.jpg"));
    }

    @Test
    @DisplayName("Test Service Error: should return 503 Service Unavailable")
    void testGetRecommendationsServiceError() throws Exception {
        // Implement test logic here
        String mockUserId = "u123";

        when(grpcClientService.getRecommendations(anyString(), anyInt()))
                .thenThrow(new RuntimeException("RPC call to the inference service failed"));

        mockMvc.perform(get("/api/recommendation/recommend").param("userId", mockUserId).param("k", "16"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(ResultCode.SERVICE_UNAVAILABLE.getCode()))
                .andExpect(jsonPath("$.message").value("Service Unavailable: RPC call to the inference service failed"))
                .andExpect(jsonPath("$.data").doesNotExist());
    }

    @Test
    @DisplayName("Test Missing Param: should return 400 Bad Request")
    void testGetRecommendationsMissingParams() throws Exception {
        mockMvc.perform(get("/api/recommendation/recommend"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(ResultCode.BAD_REQUEST.getCode()))
                .andExpect(jsonPath("$.message").value("Required parameter missing: userId"));
    }
}
