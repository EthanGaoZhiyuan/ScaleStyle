package com.scalestyle.gateway.exception;

/**
 * Business validation exception with client-safe error messages.
 * 
 * This exception indicates a client error (4xx) where the error message
 * is designed to be safe for external consumption and can be returned
 * directly to API consumers.
 * 
 * Use this for:
 * - Domain validation failures (e.g., "Invalid search mode")
 * - Business rule violations (e.g., "User ID is required")
 * - Input constraint violations (e.g., "Query exceeds maximum length")
 * 
 * Do NOT use this for internal errors or exceptions that might contain:
 * - Internal implementation details
 * - Database schema information
 * - Service endpoint URLs
 * - Stack traces or system paths
 */
public class BusinessValidationException extends RuntimeException {
    
    public BusinessValidationException(String message) {
        super(message);
    }
    
    public BusinessValidationException(String message, Throwable cause) {
        super(message, cause);
    }
}
