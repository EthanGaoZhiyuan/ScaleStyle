package com.scalestyle.gateway.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.scalestyle.gateway.dto.RecommendationDTO;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.serializer.Jackson2JsonRedisSerializer;
import org.springframework.data.redis.serializer.StringRedisSerializer;


@Configuration
public class RedisConfig {

    /**
     * String-based template for popular items and fallback data access.
     */
    @Bean
    public StringRedisTemplate stringRedisTemplate(RedisConnectionFactory connectionFactory) {
        return new StringRedisTemplate(connectionFactory);
    }

    /**
     * Typed template for product metadata cache with JSON serialization.
     */
    @Bean
    public RedisTemplate<String, RecommendationDTO> productRedisTemplate(RedisConnectionFactory connectionFactory) {
        RedisTemplate<String, RecommendationDTO> template = new RedisTemplate<>();
        template.setConnectionFactory(connectionFactory);

        // Key serializer - use String
        StringRedisSerializer keySerializer = new StringRedisSerializer();
        template.setKeySerializer(keySerializer);
        template.setHashKeySerializer(keySerializer);

        // Value serializer - use Jackson JSON for RecommendationDTO
        ObjectMapper objectMapper = new ObjectMapper();
        objectMapper.registerModule(new JavaTimeModule());

        Jackson2JsonRedisSerializer<RecommendationDTO> valueSerializer = 
                new Jackson2JsonRedisSerializer<>(objectMapper, RecommendationDTO.class);
        
        template.setValueSerializer(valueSerializer);
        template.setHashValueSerializer(valueSerializer);
        
        template.afterPropertiesSet();
        return template;
    }
    
    /**
     * ObjectMapper for recommendation cache serialization.
     * Handles generic collections via explicit TypeReference bindings.
     */
    @Bean
    public ObjectMapper recommendationCacheObjectMapper() {
        ObjectMapper mapper = new ObjectMapper();
        mapper.registerModule(new JavaTimeModule());
        return mapper;
    }
}
