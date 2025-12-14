package com.scalestyle.gateway.dto;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class RecommendationDTO {
    private String itemId;
    private String name;
    private String category;
    private String description;
    private double price;
    private String imgUrl;
}
