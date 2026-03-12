package com.scalestyle.gateway.exception;

/**
 * Signals that the best-effort click-tracking pipeline cannot currently accept a
 * new local processing attempt.
 */
public class EventTrackingUnavailableException extends RuntimeException {

    public EventTrackingUnavailableException(String message) {
        super(message);
    }

    public EventTrackingUnavailableException(String message, Throwable cause) {
        super(message, cause);
    }
}
