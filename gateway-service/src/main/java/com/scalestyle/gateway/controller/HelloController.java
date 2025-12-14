package com.scalestyle.gateway.controller;

import com.scalestyle.gateway.exception.CommonApiResponse;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class HelloController {
    // Test endpoint to return a hello message
    @GetMapping("/hello")
    public CommonApiResponse<String> sayHello() {
        return CommonApiResponse.success("Hello, World!");
    }

    // Test endpoint to simulate an error
    @GetMapping("/test-error")
    public CommonApiResponse<String> testError(@RequestParam int num) {
        int res = 10 / num; // This will throw ArithmeticException if num is 0
        return CommonApiResponse.success("Result is: " + res);
    }
}
