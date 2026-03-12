package com.scalestyle.gateway.dto;

import java.util.Set;

/**
 * Canonical allowlist for the {@code source} field on /events/click.
 * <p>
 * This is the single source of truth shared by both the DTO layer
 * ({@link TrackClickRequest}) and the service layer ({@link
 * com.scalestyle.gateway.service.EventTrackingService}).  All additions,
 * removals, or renames to the allowlist must happen here only.
 */
public final class EventSource {

    public static final Set<String> ALLOWED = Set.of(
            "search",
            "browse",
            "recommendation",
            "image_search"
    );

    /** Regex derived from {@link #ALLOWED}, used in Bean Validation {@code @Pattern}. */
    public static final String ALLOWED_PATTERN = "search|browse|recommendation|image_search";

    private EventSource() {}
}
