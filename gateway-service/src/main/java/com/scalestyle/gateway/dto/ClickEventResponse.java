package com.scalestyle.gateway.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import io.swagger.v3.oas.annotations.media.Schema;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
@Schema(description = "Click event tracking response")
public class ClickEventResponse {

    @Schema(description = "Generated event ID for idempotency / tracing", example = "evt_abc123")
    @JsonProperty("event_id")
    private String eventId;

    @Schema(
        description = "Publish status — success means Kafka acknowledged the event.",
        example = "acknowledged_by_broker"
    )
    private String status;

    @Schema(
        description = "Human-readable message for the broker-acknowledged contract",
        example = "Click event acknowledged by Kafka broker."
    )
    private String message;

    @Schema(
        description = "Processing mode — 'broker_ack_sync' means the HTTP success path waited for broker ack.",
        example = "broker_ack_sync"
    )
    @JsonProperty("processing_mode")
    private String processingMode;
}
