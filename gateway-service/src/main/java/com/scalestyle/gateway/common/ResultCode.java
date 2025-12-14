package com.scalestyle.gateway.common;

import lombok.AllArgsConstructor;
import lombok.Getter;

@Getter
@AllArgsConstructor
public enum ResultCode {
    // --- Success ---
    SUCCESS(200, "Success"),

    // --- Client Errors (4xx) ---
    BAD_REQUEST(400, "Bad Request"),
    RESOURCE_NOT_FOUND(404, "Resource Not Found"),

    // --- Server Errors (5xx) ---
    INTERNAL_SERVER_ERROR(500, "Internal Server Error"),
    SERVICE_UNAVAILABLE(503, "Service Unavailable");

    private final int code;
    private final String message;
}
