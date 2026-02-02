package com.scalestyle.gateway.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.web.client.RestTemplateBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.client.RestTemplate;

import java.time.Duration;
import java.util.concurrent.Executor;

@Configuration
public class InferenceClientConfig {

    /**
     * Dedicated thread pool for inference service calls.
     * Isolates Ray inference from common ForkJoinPool to prevent thread starvation.
     */
    @Bean(name = "inferenceExecutor")
    public Executor inferenceExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(50);
        executor.setMaxPoolSize(100);
        executor.setQueueCapacity(500);
        executor.setThreadNamePrefix("inference-");
        executor.setRejectedExecutionHandler(new java.util.concurrent.ThreadPoolExecutor.CallerRunsPolicy());
        executor.initialize();
        return executor;
    }

    @Bean
    public RestTemplate inferenceRestTemplate(
            RestTemplateBuilder builder,
            @Value("${inference.http.connect-timeout:1000}") int connectTimeout,
            @Value("${inference.http.read-timeout:450}") int readTimeout) {
        
        // Use Spring Boot's RestTemplateBuilder with auto-configured connection pooling
        return builder
                .connectTimeout(Duration.ofMillis(connectTimeout))
                .readTimeout(Duration.ofMillis(readTimeout))
                .build();
    }

    @Bean
    public String inferenceBaseUrl(@Value("${inference.base-url}") String baseUrl) {
        return baseUrl;
    }
}
