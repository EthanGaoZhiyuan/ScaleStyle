package com.scalestyle.gateway.config;

import io.swagger.v3.oas.models.info.Info;
import io.swagger.v3.oas.models.info.License;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import io.swagger.v3.oas.models.OpenAPI;

@Configuration
public class OpenApiConfig {
    @Bean
    public OpenAPI scaleStyleOpenAPI() {
        return new OpenAPI()
                .info(new Info()
                        .title("ScaleStyle Gateway Service")
                        .description("Backend API for ScaleStyle Fashion Recommendation System")
                        .version("1.0")
                        .license(new License()
                                .name("Apache 2.0")
                                .url("http://springdoc.org")
                        )
                );
    }
}
