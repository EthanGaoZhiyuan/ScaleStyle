package com.scalestyle.gateway.config;

import com.scalestyle.gateway.dto.ClickEventV1;
import org.apache.kafka.clients.CommonClientConfigs;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.config.SaslConfigs;
import org.apache.kafka.common.config.SslConfigs;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.kafka.core.DefaultKafkaProducerFactory;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class KafkaConfigProducerSettingsTest {

    @Test
    @DisplayName("ProducerFactory uses bounded retry budget with safe idempotent settings")
    void producerFactory_usesBoundedRetryBudget() {
        KafkaConfig config = configuredKafkaConfig();

        DefaultKafkaProducerFactory<String, ClickEventV1> factory =
                (DefaultKafkaProducerFactory<String, ClickEventV1>) config.producerFactory();

        Map<String, Object> props = factory.getConfigurationProperties();

        assertThat(props.get(ProducerConfig.ACKS_CONFIG)).isEqualTo("all");
        assertThat(props.get(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG)).isEqualTo(true);
        assertThat(props.get(ProducerConfig.RETRIES_CONFIG)).isEqualTo(3);
        assertThat(props.get(ProducerConfig.REQUEST_TIMEOUT_MS_CONFIG)).isEqualTo(5000);
        assertThat(props.get(ProducerConfig.DELIVERY_TIMEOUT_MS_CONFIG)).isEqualTo(5500);
        assertThat(props.get(ProducerConfig.RETRY_BACKOFF_MS_CONFIG)).isEqualTo(250);
        assertThat(props.get(ProducerConfig.RETRY_BACKOFF_MAX_MS_CONFIG)).isEqualTo(1000);
        assertThat(props.get(ProducerConfig.LINGER_MS_CONFIG)).isEqualTo(2);
        assertThat(props.get(ProducerConfig.COMPRESSION_TYPE_CONFIG)).isEqualTo("lz4");
        assertThat(props.get(ProducerConfig.BATCH_SIZE_CONFIG)).isEqualTo(8192);
        assertThat(props.get(ProducerConfig.MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION)).isEqualTo(5);
        assertThat((Integer) props.get(ProducerConfig.DELIVERY_TIMEOUT_MS_CONFIG)).isLessThanOrEqualTo(6000);
    }

    @Test
    @DisplayName("Invalid timeout budget fails fast during producer config creation")
    void producerFactory_invalidTimeoutBudget_failsFast() {
        KafkaConfig config = configuredKafkaConfig();
        ReflectionTestUtils.setField(config, "deliveryTimeoutMs", 5000);
        ReflectionTestUtils.setField(config, "requestTimeoutMs", 5000);
        ReflectionTestUtils.setField(config, "lingerMs", 2);

        assertThatThrownBy(config::producerFactory)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("delivery-timeout-ms");
    }

    @Test
    @DisplayName("Delivery timeout cannot exceed broker ack timeout")
    void producerFactory_deliveryTimeoutCannotExceedBrokerAckTimeout() {
        KafkaConfig config = configuredKafkaConfig();
        ReflectionTestUtils.setField(config, "deliveryTimeoutMs", 6001);
        ReflectionTestUtils.setField(config, "brokerAckTimeoutMs", 6000L);

        assertThatThrownBy(config::producerFactory)
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("ack-timeout-ms");
    }

            @Test
            @DisplayName("PLAINTEXT local default omits SASL and SSL-only properties")
            void producerFactory_plaintextDefault_omitsSaslAndSslProps() {
            KafkaConfig config = configuredKafkaConfig();
            ReflectionTestUtils.setField(config, "securityProtocol", "PLAINTEXT");
            ReflectionTestUtils.setField(config, "saslMechanism", "SCRAM-SHA-512");
            ReflectionTestUtils.setField(config, "kafkaUsername", "user");
            ReflectionTestUtils.setField(config, "kafkaPassword", "pass");
            ReflectionTestUtils.setField(config, "kafkaSslCaPath", "/tmp/ca.pem");

            DefaultKafkaProducerFactory<String, ClickEventV1> factory =
                (DefaultKafkaProducerFactory<String, ClickEventV1>) config.producerFactory();

            Map<String, Object> props = factory.getConfigurationProperties();

            assertThat(props.get(CommonClientConfigs.SECURITY_PROTOCOL_CONFIG)).isEqualTo("PLAINTEXT");
            assertThat(props).doesNotContainKeys(
                SaslConfigs.SASL_MECHANISM,
                SaslConfigs.SASL_JAAS_CONFIG,
                SslConfigs.SSL_TRUSTSTORE_TYPE_CONFIG,
                SslConfigs.SSL_TRUSTSTORE_LOCATION_CONFIG,
                SslConfigs.SSL_ENDPOINT_IDENTIFICATION_ALGORITHM_CONFIG
            );
            }

    private KafkaConfig configuredKafkaConfig() {
        KafkaConfig config = new KafkaConfig();
        ReflectionTestUtils.setField(config, "bootstrapServers", "localhost:9092");
        ReflectionTestUtils.setField(config, "securityProtocol", "");
        ReflectionTestUtils.setField(config, "saslMechanism", "");
        ReflectionTestUtils.setField(config, "kafkaUsername", "");
        ReflectionTestUtils.setField(config, "kafkaPassword", "");
        ReflectionTestUtils.setField(config, "kafkaSslCaPath", "");
        ReflectionTestUtils.setField(config, "producerRetries", 3);
        ReflectionTestUtils.setField(config, "requestTimeoutMs", 5000);
        ReflectionTestUtils.setField(config, "deliveryTimeoutMs", 5500);
        ReflectionTestUtils.setField(config, "brokerAckTimeoutMs", 6000L);
        ReflectionTestUtils.setField(config, "retryBackoffMs", 250);
        ReflectionTestUtils.setField(config, "retryBackoffMaxMs", 1000);
        ReflectionTestUtils.setField(config, "lingerMs", 2);
        ReflectionTestUtils.setField(config, "batchSizeBytes", 8192);
        ReflectionTestUtils.setField(config, "maxInFlightRequests", 5);
        ReflectionTestUtils.setField(config, "compressionType", "lz4");
        return config;
    }
}
