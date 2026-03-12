package com.scalestyle.gateway.config;

import com.scalestyle.gateway.dto.ClickEventV1;

import org.apache.kafka.clients.CommonClientConfigs;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.config.SaslConfigs;
import org.apache.kafka.common.config.SslConfigs;
import org.apache.kafka.common.serialization.StringSerializer;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.core.DefaultKafkaProducerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.core.ProducerFactory;
import org.springframework.kafka.support.serializer.JsonSerializer;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.util.StringUtils;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.Executor;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * Kafka producer configuration for best-effort click tracking.
 * Broker acks are observed asynchronously and must not be inferred from the HTTP response.
 */
@Configuration
public class KafkaConfig {
    
    @Value("${spring.kafka.bootstrap-servers:localhost:9092}")
    private String bootstrapServers;

    @Value("${KAFKA_SECURITY_PROTOCOL:PLAINTEXT}")
    private String securityProtocol;

    @Value("${KAFKA_SASL_MECHANISM:SCRAM-SHA-512}")
    private String saslMechanism;

    @Value("${KAFKA_USERNAME:}")
    private String kafkaUsername;

    @Value("${KAFKA_PASSWORD:}")
    private String kafkaPassword;

    @Value("${KAFKA_SSL_CA_PATH:}")
    private String kafkaSslCaPath;

    @Value("${event.producer.kafka.retries:3}")
    private int producerRetries;

    @Value("${event.producer.kafka.request-timeout-ms:5000}")
    private int requestTimeoutMs;

    @Value("${event.producer.kafka.delivery-timeout-ms:15000}")
    private int deliveryTimeoutMs;

    @Value("${event.producer.kafka.ack-timeout-ms:6000}")
    private long brokerAckTimeoutMs;

    @Value("${event.producer.kafka.retry-backoff-ms:250}")
    private int retryBackoffMs;

    @Value("${event.producer.kafka.retry-backoff-max-ms:1000}")
    private int retryBackoffMaxMs;

    @Value("${event.producer.kafka.linger-ms:2}")
    private int lingerMs;

    @Value("${event.producer.kafka.batch-size-bytes:8192}")
    private int batchSizeBytes;

    @Value("${event.producer.kafka.max-in-flight-requests:5}")
    private int maxInFlightRequests;

    @Value("${event.producer.kafka.compression-type:lz4}")
    private String compressionType;
    
    @Bean
    public ProducerFactory<String, ClickEventV1> producerFactory() {
        Map<String, Object> configProps = new HashMap<>();
        configProps.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        configProps.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        configProps.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, JsonSerializer.class);
        
        validateProducerTimeoutBudget();

        // Best-effort tracking still needs safe broker semantics:
        // - acks=all: broker success only after ISR acknowledgment
        // - idempotence: duplicate suppression during bounded retries
        // - bounded retries: prompt failure visibility instead of opaque long tails
        // - max.in.flight.requests=5: preserves ordering guarantees with idempotence
        configProps.put(ProducerConfig.ACKS_CONFIG, "all");
        configProps.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true);
        configProps.put(ProducerConfig.RETRIES_CONFIG, producerRetries);
        configProps.put(ProducerConfig.MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION, maxInFlightRequests);
        
        // Bound how long send() may block the calling thread waiting for metadata
        // or buffer space.  200 ms is enough for healthy brokers; if the producer
        // cannot proceed within this window it throws immediately so the bulkhead
        // executor thread is freed rather than parked indefinitely.
        configProps.put(ProducerConfig.MAX_BLOCK_MS_CONFIG, 200);

        // Explicit buffer ceiling so the producer never silently grows without
        // bound.  32 MiB covers ~2 000 click events (≈16 KiB each) in-flight.
        configProps.put(ProducerConfig.BUFFER_MEMORY_CONFIG, 32 * 1024 * 1024L);

        // Timeout settings: surface failures within a bounded operational window.
        // request.timeout.ms bounds one broker attempt; delivery.timeout.ms bounds the
        // total wall-clock send budget across all retries.
        configProps.put(ProducerConfig.REQUEST_TIMEOUT_MS_CONFIG, requestTimeoutMs);
        configProps.put(ProducerConfig.DELIVERY_TIMEOUT_MS_CONFIG, deliveryTimeoutMs);
        configProps.put(ProducerConfig.RETRY_BACKOFF_MS_CONFIG, retryBackoffMs);
        configProps.put(ProducerConfig.RETRY_BACKOFF_MAX_MS_CONFIG, retryBackoffMaxMs);
        
        // Small click events are latency-sensitive and rarely fill large batches:
        // - lz4 gives materially lower CPU cost than gzip for tiny JSON payloads
        // - 2 ms linger preserves a little coalescing without adding a visible tail
        // - 8 KiB batches fit small bursts without holding records for "big batch" throughput goals
        configProps.put(ProducerConfig.COMPRESSION_TYPE_CONFIG, compressionType);
        configProps.put(ProducerConfig.BATCH_SIZE_CONFIG, batchSizeBytes);
        configProps.put(ProducerConfig.LINGER_MS_CONFIG, lingerMs);

        String normalizedSecurityProtocol = StringUtils.hasText(securityProtocol)
                ? securityProtocol.trim().toUpperCase()
                : "";
        boolean saslEnabled = normalizedSecurityProtocol.startsWith("SASL_");
        boolean sslEnabled = normalizedSecurityProtocol.contains("SSL");

        if (StringUtils.hasText(normalizedSecurityProtocol)) {
            configProps.put(CommonClientConfigs.SECURITY_PROTOCOL_CONFIG, normalizedSecurityProtocol);
        }
        if (saslEnabled && StringUtils.hasText(saslMechanism)) {
            configProps.put(SaslConfigs.SASL_MECHANISM, saslMechanism);
        }
        if (saslEnabled && StringUtils.hasText(kafkaUsername) && StringUtils.hasText(kafkaPassword)) {
            configProps.put(
                    SaslConfigs.SASL_JAAS_CONFIG,
                    String.format(
                            "org.apache.kafka.common.security.scram.ScramLoginModule required username=\"%s\" password=\"%s\";",
                            kafkaUsername,
                            kafkaPassword
                    )
            );
        }
        if (sslEnabled && StringUtils.hasText(kafkaSslCaPath)) {
            configProps.put(SslConfigs.SSL_TRUSTSTORE_TYPE_CONFIG, "PEM");
            configProps.put(SslConfigs.SSL_TRUSTSTORE_LOCATION_CONFIG, kafkaSslCaPath);
            configProps.put(SslConfigs.SSL_ENDPOINT_IDENTIFICATION_ALGORITHM_CONFIG, "HTTPS");
        }
        
        return new DefaultKafkaProducerFactory<>(configProps);
    }

    private void validateProducerTimeoutBudget() {
        if (producerRetries < 0) {
            throw new IllegalArgumentException("event.producer.kafka.retries must be >= 0");
        }
        if (requestTimeoutMs <= 0 || deliveryTimeoutMs <= 0 || brokerAckTimeoutMs <= 0) {
            throw new IllegalArgumentException("Kafka producer timeouts must be > 0");
        }
        if (deliveryTimeoutMs <= requestTimeoutMs + lingerMs) {
            throw new IllegalArgumentException(
                    "event.producer.kafka.delivery-timeout-ms must be greater than request-timeout-ms + linger-ms"
            );
        }
        if (deliveryTimeoutMs > brokerAckTimeoutMs) {
            throw new IllegalArgumentException(
                    "event.producer.kafka.delivery-timeout-ms must be <= event.producer.kafka.ack-timeout-ms"
            );
        }
        if (maxInFlightRequests <= 0 || maxInFlightRequests > 5) {
            throw new IllegalArgumentException(
                    "event.producer.kafka.max-in-flight-requests must be between 1 and 5 when idempotence is enabled"
            );
        }
    }
    
    @Bean
    public KafkaTemplate<String, ClickEventV1> kafkaTemplate() {
        return new KafkaTemplate<>(producerFactory());
    }

    /**
        * Dedicated local executor for broker-acknowledged click-tracking attempts.
        *
        * Admission to this executor is the controller's bulkhead boundary: the
        * Tomcat thread is released immediately, but HTTP success still requires the
        * publish to reach Kafka broker acknowledgment inside one of these workers.
     */
    @Bean(name = "eventProducerExecutor")
    public Executor eventProducerExecutor(
            @Value("${event.producer.executor.core-pool-size:4}") int corePoolSize,
            @Value("${event.producer.executor.max-pool-size:8}") int maxPoolSize,
            @Value("${event.producer.executor.queue-capacity:200}") int queueCapacity
    ) {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(corePoolSize);
        executor.setMaxPoolSize(maxPoolSize);
        executor.setQueueCapacity(queueCapacity);
        executor.setThreadNamePrefix("click-tracking-");
        executor.setRejectedExecutionHandler(new ThreadPoolExecutor.AbortPolicy());
        executor.initialize();
        return executor;
    }
}
