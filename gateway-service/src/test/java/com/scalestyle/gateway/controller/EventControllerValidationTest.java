package com.scalestyle.gateway.controller;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

import com.scalestyle.gateway.dto.ClickEvent;
import com.scalestyle.gateway.exception.CommonApiResponse;
import com.scalestyle.gateway.service.EventProducerService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.Map;

/**
 * Test EventController boundary validation
 * Week 2 - P0 hardening: Device, position, source, event_type validation
 */
@ExtendWith(MockitoExtension.class)
class EventControllerValidationTest {
    
    @Mock
    private EventProducerService eventProducerService;
    
    @InjectMocks
    private EventController eventController;
    
    private ClickEvent validEvent;
    
    @BeforeEach
    void setUp() {
        validEvent = ClickEvent.builder()
                .userId("user_12345")
                .itemId("108775051")
                .sessionId("sess_abc123")
                .source("search")
                .query("red dress")
                .position(2)
                .build();
    }
    
    @Test
    void testValidEventAccepted() {
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(200, response.getCode());
        assertEquals("accepted", response.getData().get("status"));
        verify(eventProducerService, times(1)).publishClickEvent(any(ClickEvent.class));
    }
    
    @Test
    void testMissingUserIdRejected() {
        // Arrange
        validEvent.setUserId(null);
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("user_id"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testEmptyUserIdRejected() {
        // Arrange
        validEvent.setUserId("  ");
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("user_id"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testMissingItemIdRejected() {
        // Arrange
        validEvent.setItemId(null);
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("item_id"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testMissingSessionIdRejected() {
        // Arrange
        validEvent.setSessionId(null);
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("session_id"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testInvalidSourceRejected() {
        // Arrange
        validEvent.setSource("invalid_source");
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("source"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testValidSourcesAccepted() {
        String[] validSources = {"search", "browse", "recommendation", "image_search"};
        
        for (String source : validSources) {
            validEvent.setSource(source);
            CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
            assertEquals(200, response.getCode(), "Source '" + source + "' should be accepted");
        }
        
        verify(eventProducerService, times(validSources.length)).publishClickEvent(any());
    }
    
    @Test
    void testInvalidEventTypeRejected() {
        // Arrange
        validEvent.setEventType("purchase");
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("event_type"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testQueryTooLongRejected() {
        // Arrange - create query > 500 chars
        String longQuery = "a".repeat(501);
        validEvent.setQuery(longQuery);
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("query"));
        assertTrue(response.getMessage().contains("500"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testImageHashTooLongRejected() {
        // Arrange - create image_hash > 128 chars
        String longHash = "x".repeat(129);
        validEvent.setImageHash(longHash);
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("image_hash"));
        assertTrue(response.getMessage().contains("128"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testInvalidTimestampRejected() {
        // Arrange
        validEvent.setTimestamp("not-a-timestamp");
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("timestamp"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testValidTimestampAccepted() {
        // Arrange
        validEvent.setTimestamp("2026-03-07T10:30:45.123Z");
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(200, response.getCode());
        verify(eventProducerService, times(1)).publishClickEvent(any());
    }
    
    @Test
    void testMissingTimestampAutoFilled() {
        // Arrange
        validEvent.setTimestamp(null);
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(200, response.getCode());
        assertNotNull(validEvent.getTimestamp());
        verify(eventProducerService, times(1)).publishClickEvent(any());
    }
    
    @Test
    void testInvalidDeviceRejected() {
        // Arrange
        validEvent.setDevice("xbox");  // Invalid device
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("device"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testValidDevicesAccepted() {
        String[] validDevices = {"web", "mobile", "api", "WEB", "Mobile", "API"};
        
        for (String device : validDevices) {
            validEvent.setDevice(device);
            CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
            assertEquals(200, response.getCode(), "Device '" + device + "' should be accepted");
        }
        
        verify(eventProducerService, times(validDevices.length)).publishClickEvent(any());
    }
    
    @Test
    void testNegativePositionRejected() {
        // Arrange
        validEvent.setPosition(-1);  // Negative position
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(400, response.getCode());
        assertTrue(response.getMessage().contains("position"));
        verify(eventProducerService, never()).publishClickEvent(any());
    }
    
    @Test
    void testZeroPositionAccepted() {
        // Arrange - position=0 is valid (0-indexed)
        validEvent.setPosition(0);
        
        // Act
        CommonApiResponse<Map<String, Object>> response = eventController.trackClick(validEvent);
        
        // Assert
        assertEquals(200, response.getCode());
        verify(eventProducerService, times(1)).publishClickEvent(any());
    }
}
