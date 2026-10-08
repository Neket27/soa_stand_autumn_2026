package orders.inventory;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.time.Duration;

import static org.junit.jupiter.api.Assertions.*;

class InventoryClientTest {

    private HttpServer mockServer;
    private int port;
    private InventoryClient client;

    @BeforeEach
    void setUp() throws IOException {
        mockServer = HttpServer.create(new InetSocketAddress(0), 0);
        port = mockServer.getAddress().getPort();

        // Handle POST /reservations
        mockServer.createContext("/reservations", exchange -> {
            if ("POST".equals(exchange.getRequestMethod())) {
                String body = new String(exchange.getRequestBody().readAllBytes());
                if (body.contains("\"order_id\":\"order-reserved\"")) {
                    String response = "{\"order_id\":\"order-reserved\",\"status\":\"held\",\"items\":[]}";
                    exchange.getResponseHeaders().add("Content-Type", "application/json");
                    exchange.sendResponseHeaders(201, response.length());
                    exchange.getResponseBody().write(response.getBytes());
                } else {
                    // Default: success
                    String response = "{\"order_id\":\"unknown\",\"status\":\"held\",\"items\":[]}";
                    exchange.getResponseHeaders().add("Content-Type", "application/json");
                    exchange.sendResponseHeaders(201, response.length());
                    exchange.getResponseBody().write(response.getBytes());
                }
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
    void reserve_success() {
        boolean reserved = client.reserve("order-reserved", "[{\"sku\":\"sku-1\",\"qty\":2,\"price_cents\":100}]", Duration.ofSeconds(5));
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
