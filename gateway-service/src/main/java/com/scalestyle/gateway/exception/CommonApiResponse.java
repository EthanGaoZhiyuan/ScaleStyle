package com.scalestyle.gateway.exception;

import com.scalestyle.gateway.common.ResultCode;
import lombok.Data;

@Data
public class CommonApiResponse<T> {
    private int code;           // HTTP status code (e.g., 200 for success, 400 for client error, 500 for server error)
    private String message;     // Response message (e.g., "Success", "Error occurred")
    private T data;             // Response data (generic type)

    // private constructor
    private CommonApiResponse(int code, String message, T data) {
        this.code = code;
        this.message = message;
        this.data = data;
    }

    // static helper method for success response
    public static <T> CommonApiResponse<T> success(T data) {
        return new CommonApiResponse<>(ResultCode.SUCCESS.getCode(), ResultCode.SUCCESS.getMessage(), data);
    }

    // static helper method for error response
    public static <T> CommonApiResponse<T> error(int code, String message) {
        return new CommonApiResponse<>(code, message, null);
    }
}
