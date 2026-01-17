package com.scalestyle.gateway.exception;

public class InferenceServiceException extends RuntimeException {
    public InferenceServiceException(String message) {
        super(message);
    }
    public InferenceServiceException(String message, Throwable cause) {
        super(message, cause);
    }
}
