package com.scalestyle.gateway.handler;

import com.scalestyle.gateway.common.ResultCode;
import com.scalestyle.gateway.exception.CommonApiResponse;
import com.scalestyle.gateway.exception.InferenceServiceException;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.resource.NoResourceFoundException;

@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger logger = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    // Catch all handler fofr unknown exceptions (Exception.class)
    @ExceptionHandler(Exception.class)
    public CommonApiResponse<String> handleException(Exception ex) {
        logger.error("Unhandled exception caught: ", ex);
        return CommonApiResponse.error(ResultCode.INTERNAL_SERVER_ERROR.getCode(), ResultCode.INTERNAL_SERVER_ERROR.getMessage() + ex.getMessage());
    }

    @ExceptionHandler(InferenceServiceException.class)
    public CommonApiResponse<String> handleInferenceServiceException(InferenceServiceException e) {
        logger.error("Inference service error: ", e);
        return CommonApiResponse.error(ResultCode.SERVICE_UNAVAILABLE.getCode(),
                ResultCode.SERVICE_UNAVAILABLE.getMessage() + ": " + e.getMessage());
    }

    @ExceptionHandler(NoResourceFoundException.class)
    public CommonApiResponse<String> handleNoResourceFoundException(NoResourceFoundException ex) {
        logger.error("Resource not found: ", ex);
        return CommonApiResponse.error(ResultCode.RESOURCE_NOT_FOUND.getCode(), ResultCode.RESOURCE_NOT_FOUND.getMessage() + ex.getMessage());
    }

    @ExceptionHandler(MissingServletRequestParameterException.class)
    public CommonApiResponse<String> handleMissingParams(MissingServletRequestParameterException e) {
        return CommonApiResponse.error(ResultCode.BAD_REQUEST.getCode(), "Required parameter missing: " + e.getParameterName());
    }

    @ExceptionHandler(RuntimeException.class)
    @ResponseStatus(HttpStatus.OK)
    public CommonApiResponse<String> handleRuntimeException(RuntimeException e) {
        logger.error("Runtime exception caught: ", e);

        if (e.getMessage() != null && e.getMessage().contains("RPC call")) {
            return CommonApiResponse.error(ResultCode.SERVICE_UNAVAILABLE.getCode(), "Service Unavailable: " + e.getMessage());
        }

        return CommonApiResponse.error(ResultCode.INTERNAL_SERVER_ERROR.getCode(), "Internal Server Error: " + e.getMessage());
    }
}
