package orders.api;

import orders.api.dto.ApiError;
import org.junit.jupiter.api.Test;
import org.springframework.http.ResponseEntity;

import static org.junit.jupiter.api.Assertions.*;

class GlobalExceptionHandlerTest {

    private final GlobalExceptionHandler handler = new GlobalExceptionHandler();

    @Test
    void handleNotFound_returns404WithErrorBody() {
        ResponseEntity<ApiError> response =
                handler.handleNotFound(new OrderNotFoundException("order-1"));

        assertEquals(404, response.getStatusCode().value());
        ApiError body = response.getBody();
        assertNotNull(body);
        assertEquals("not_found", body.error());
        assertEquals("order not found", body.message());
    }

    @Test
    void handleNotFound_messageContainsOrderId() {
        OrderNotFoundException ex = new OrderNotFoundException("id-42");

        assertEquals("order not found: id-42", ex.getMessage());
        assertEquals("id-42", ex.getOrderId());
    }
}
