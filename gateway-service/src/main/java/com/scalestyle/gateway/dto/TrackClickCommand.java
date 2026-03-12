package com.scalestyle.gateway.dto;

/**
 * Domain command for click tracking.
 * Keeps service logic independent from transport and event contract models.
 */
public record TrackClickCommand(
        String userId,
        String itemId,
        String sessionId,
        String source,
        String query,
        String imageHash,
        Integer position,
        String device
) {
}
