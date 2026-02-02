package com.scalestyle.gateway.config;

import com.fasterxml.jackson.annotation.JsonTypeInfo;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.jsontype.BasicPolymorphicTypeValidator;
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
     * StringRedisTemplate for accessing popular items (global:popular ZSET).
     * Reused across fallback calls to avoid repeated instantiation.
     */
    @Bean
    public StringRedisTemplate stringRedisTemplate(RedisConnectionFactory connectionFactory) {
        return new StringRedisTemplate(connectionFactory);
    }

    @Bean
    public RedisTemplate<String, RecommendationDTO> productRedisTemplate(RedisConnectionFactory connectionFactory) {
        RedisTemplate<String, RecommendationDTO> template = new RedisTemplate<>();
        template.setConnectionFactory(connectionFactory);

        // Key serializer - use String
        StringRedisSerializer keySerializer = new StringRedisSerializer();
        template.setKeySerializer(keySerializer);
        template.setHashKeySerializer(keySerializer);

        // Value serializer - use Jackson JSON
        // Security: Restrict polymorphic types to DTO package only (not Object.class)
        ObjectMapper objectMapper = new ObjectMapper();
        objectMapper.registerModule(new JavaTimeModule());
        objectMapper.activateDefaultTyping(
                BasicPolymorphicTypeValidator.builder()
                        .allowIfBaseType(RecommendationDTO.class)
                        .build(),
                ObjectMapper.DefaultTyping.NON_FINAL,
                JsonTypeInfo.As.PROPERTY
        );

        Jackson2JsonRedisSerializer<RecommendationDTO> valueSerializer = 
                new Jackson2JsonRedisSerializer<>(objectMapper, RecommendationDTO.class);
        
        template.setValueSerializer(valueSerializer);
        template.setHashValueSerializer(valueSerializer);
        
        template.afterPropertiesSet();
        return template;
    }
    
    /**
     * P0-2 Fix: ObjectMapper bean for manual JSON serialization of List<RecommendationDTO>.
     * This approach avoids Jackson's default typing issues with generic collections.
     * Used by RecommendationService to serialize/deserialize cache values as JSON strings.
     */
    @Bean
    public ObjectMapper recommendationCacheObjectMapper() {
        ObjectMapper mapper = new ObjectMapper();
        mapper.registerModule(new JavaTimeModule());
        // No default typing needed - explicit TypeReference handles generic types safely
        return mapper;
    }
}
