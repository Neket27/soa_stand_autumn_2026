package orders.payment;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.time.Duration;

import static org.junit.jupiter.api.Assertions.*;

class PaymentClientTest {

    private HttpServer mockServer;
    private int port;
    private PaymentClient client;

    @BeforeEach
    void setUp() throws IOException {
        mockServer = HttpServer.create(new InetSocketAddress(0), 0);
        port = mockServer.getAddress().getPort();

        mockServer.createContext("/payments", exchange -> {
            String method = exchange.getRequestMethod();
            String idempotencyKey = exchange.getRequestHeaders().getFirst("Idempotency-Key");
            String body = new String(exchange.getRequestBody().readAllBytes());
            String query = exchange.getRequestURI().getQuery();

            if ("POST".equals(method)) {
                if (body.contains("\"amount_cents\":42")) {
                    exchange.sendResponseHeaders(500, 0);
                } else if (body.contains("\"amount_cents\":43")) {
                    exchange.getResponseHeaders().add("Content-Type", "application/json");
                    exchange.sendResponseHeaders(402, 0);
                } else if (body.contains("\"amount_cents\":41")) {
                    String response = String.format(
                            "{\"charge_id\":\"ch-1\",\"order_id\":\"%s\",\"amount_cents\":41,\"currency\":\"RUB\",\"status\":\"captured\"}",
                            idempotencyKey);
                    exchange.getResponseHeaders().add("Content-Type", "application/json");
                    exchange.sendResponseHeaders(201, response.length());
                    exchange.getResponseBody().write(response.getBytes());
                } else {
                    String response = String.format(
                            "{\"charge_id\":\"ch-ok\",\"order_id\":\"%s\",\"amount_cents\":0,\"currency\":\"RUB\",\"status\":\"captured\"}",
                            idempotencyKey);
                    exchange.getResponseHeaders().add("Content-Type", "application/json");
                    exchange.sendResponseHeaders(201, response.length());
                    exchange.getResponseBody().write(response.getBytes());
                }
                exchange.getResponseBody().close();
            } else if ("GET".equals(method)) {
                if (query != null && query.contains("order_id=test-charged")) {
                    String response = "[{\"charge_id\":\"ch-1\",\"order_id\":\"test-charged\",\"amount_cents\":100,\"currency\":\"RUB\",\"status\":\"captured\"}]";
                    exchange.getResponseHeaders().add("Content-Type", "application/json");
                    exchange.sendResponseHeaders(200, response.length());
                    exchange.getResponseBody().write(response.getBytes());
                } else if (query != null && query.contains("order_id=test-empty")) {
                    String response = "[]";
                    exchange.getResponseHeaders().add("Content-Type", "application/json");
                    exchange.sendResponseHeaders(200, response.length());
                    exchange.getResponseBody().write(response.getBytes());
                } else if (query != null && query.contains("order_id=test-500")) {
                    exchange.sendResponseHeaders(500, 0);
                    exchange.getResponseBody().close();
                } else {
                    exchange.sendResponseHeaders(200, 0);
                    exchange.getResponseBody().close();
                }
            }
        });

        mockServer.start();
        client = new PaymentClient("http://localhost:" + port, 5000, new com.fasterxml.jackson.databind.ObjectMapper());
    }

    @AfterEach
    void tearDown() {
        mockServer.stop(0);
    }

    @Test
    void charge_success() {
        PaymentClient.ChargeResult result = client.charge("order-1", 41, Duration.ofSeconds(5));
        assertEquals(PaymentClient.ChargeResult.CHARGED, result);
    }

    @Test
    void charge_serverError() {
        PaymentClient.ChargeResult result = client.charge("order-2", 42, Duration.ofSeconds(5));
        assertEquals(PaymentClient.ChargeResult.SERVER_ERROR, result);
    }

    @Test
    void charge_declined() {
        PaymentClient.ChargeResult result = client.charge("order-3", 43, Duration.ofSeconds(5));
        assertEquals(PaymentClient.ChargeResult.DECLINED, result);
    }

    @Test
    void reconcile_charged() {
        assertEquals(PaymentClient.ReconciliationResult.CHARGED,
                client.reconcile("test-charged", Duration.ofSeconds(5)));
    }

    @Test
    void reconcile_notCharged() {
        assertEquals(PaymentClient.ReconciliationResult.NOT_CHARGED,
                client.reconcile("test-empty", Duration.ofSeconds(5)));
    }

    @Test
    void reconcile_unknown_onServerError() {
        assertEquals(PaymentClient.ReconciliationResult.UNKNOWN,
                client.reconcile("test-500", Duration.ofSeconds(5)));
    }

    @Test
    void reconcile_unknown_onConnectionError() {
        PaymentClient downClient = new PaymentClient("http://localhost:1", 200, new com.fasterxml.jackson.databind.ObjectMapper());
        assertEquals(PaymentClient.ReconciliationResult.UNKNOWN,
                downClient.reconcile("any-order", Duration.ofSeconds(2)));
    }

    @Test
    void charge_sendsIdempotencyKey() {
        PaymentClient.ChargeResult result = client.charge("test-order-id", 41, Duration.ofSeconds(5));
        assertEquals(PaymentClient.ChargeResult.CHARGED, result);
    }
}
