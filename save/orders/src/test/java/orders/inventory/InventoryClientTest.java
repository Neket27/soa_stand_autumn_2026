package orders.inventory;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.time.Duration;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

class InventoryClientTest {

    private HttpServer mockServer;
    private int port;
    private InventoryClient client;

    @BeforeEach
    void setUp() throws IOException {
        mockServer = HttpServer.create(new InetSocketAddress(0), 0);
        port = mockServer.getAddress().getPort();

        // Handle POST /reservations: 201 only if the body carries order_id
        // and the typed items serialized in the contract shape.
        mockServer.createContext("/reservations", exchange -> {
            if ("POST".equals(exchange.getRequestMethod())) {
                String body = new String(exchange.getRequestBody().readAllBytes());
                boolean ok = body.contains("\"order_id\":\"order-reserved\"")
                        && body.contains("\"sku\":\"sku-1\"")
                        && body.contains("\"qty\":2}");
                int status = ok ? 201 : 400;
                String response = "{\"order_id\":\"order-reserved\",\"status\":\"held\",\"items\":[]}";
                exchange.getResponseHeaders().add("Content-Type", "application/json");
                exchange.sendResponseHeaders(status, response.length());
                exchange.getResponseBody().write(response.getBytes());
                exchange.getResponseBody().close();
            }
        });

        // Handle DELETE /reservations/{orderId}
        mockServer.createContext("/reservations/", exchange -> {
            if ("DELETE".equals(exchange.getRequestMethod())) {
                exchange.sendResponseHeaders(204, -1);
            }
        });

        mockServer.start();
        client = new InventoryClient("http://localhost:" + port, 5000, new com.fasterxml.jackson.databind.ObjectMapper());
    }

    @AfterEach
    void tearDown() {
        mockServer.stop(0);
    }

    @Test
    void reserve_success_bodyCarriesOrderIdAndTypedItems() {
        boolean reserved = client.reserve("order-reserved",
                List.of(new orders.api.dto.Item("sku-1", 2, new java.math.BigDecimal("100"))), Duration.ofSeconds(5));
        assertTrue(reserved);
    }

    @Test
    void release_success() {
        boolean released = client.release("order-reserved", Duration.ofSeconds(5));
        assertTrue(released);
    }

    @Test
    void release_isIdempotent() {
        // Second release should also succeed (204)
        boolean first = client.release("order-reserved", Duration.ofSeconds(5));
        boolean second = client.release("order-reserved", Duration.ofSeconds(5));
        assertTrue(first);
        assertTrue(second);
    }
}
