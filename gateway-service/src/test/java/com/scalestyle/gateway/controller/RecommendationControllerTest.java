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
import java.util.concurrent.CompletableFuture;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.asyncDispatch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(RecommendationController.class)
class RecommendationControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private RecommendationService recommendationService;

    @Test
    @DisplayName("GET /api/recommendation/search - Success")
    void testSearchSuccess() throws Exception {
        RecommendationDTO mockDetail = RecommendationDTO.builder()
                .itemId("0706016001")
                .name("Test Black Jeans")
                .category("Trousers")
                .description("desc")
                .price(29.99)
                .imgUrl("http://mock.url/img.jpg")
                .build();

        when(recommendationService.searchAsync(eq("black jeans"), nullable(String.class), nullable(String.class), eq(5), eq(false)))
                .thenReturn(CompletableFuture.completedFuture(List.of(mockDetail)));

        var mvcResult = mockMvc.perform(get("/api/recommendation/search")
                        .param("query", "black jeans")
                        .param("k", "5"))
                .andExpect(request().asyncStarted())
                .andReturn();

        mockMvc.perform(asyncDispatch(mvcResult))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(ResultCode.SUCCESS.getCode()))
                .andExpect(jsonPath("$.message").value(ResultCode.SUCCESS.getMessage()))
                .andExpect(jsonPath("$.data[0].itemId").value("0706016001"))
                .andExpect(jsonPath("$.data[0].name").value("Test Black Jeans"))
                .andExpect(jsonPath("$.data[0].price").value(29.99))
                .andExpect(jsonPath("$.data[0].imgUrl").value("http://mock.url/img.jpg"));
    }

    @Test
    @DisplayName("GET /api/recommendation/search - Missing required param query")
    void testSearchMissingQuery() throws Exception {
        mockMvc.perform(get("/api/recommendation/search"))
                .andExpect(status().isBadRequest());
    }

    @Test
    @DisplayName("GET /api/recommendation/search - Service throws exception")
    void testSearchServiceError() throws Exception {
        when(recommendationService.searchAsync(anyString(), any(), any(), anyInt(), anyBoolean()))
                .thenReturn(CompletableFuture.failedFuture(new RuntimeException("Inference service call failed")));

        var mvcResult = mockMvc.perform(get("/api/recommendation/search")
                        .param("query", "x"))
                .andExpect(request().asyncStarted())
                .andReturn();

        mockMvc.perform(asyncDispatch(mvcResult))
                .andExpect(status().isInternalServerError());
    }
}
