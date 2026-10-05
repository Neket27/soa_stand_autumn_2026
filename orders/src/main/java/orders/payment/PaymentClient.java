package orders.payment;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

public class PaymentClient {

    private final HttpClient httpClient;
    private final HttpClient lookupClient;
    private final String baseUrl;
    private final ObjectMapper objectMapper;

    public PaymentClient(String baseUrl, int connectTimeoutMs, int readTimeoutMs) {
        this.baseUrl = baseUrl;
        this.objectMapper = new ObjectMapper();
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(connectTimeoutMs))
                .build();
        // Separate client for hasCharges with its own connection pool
        // to avoid reuse of broken connections after charge timeouts.
        this.lookupClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(connectTimeoutMs))
                .build();
    }

    public ChargeResult charge(String orderId, long amountCents, Duration timeout) {
        String body = String.format(
                "{\"order_id\":\"%s\",\"amount_cents\":%d,\"currency\":\"RUB\"}",
                orderId, amountCents);

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/payments"))
                .header("Content-Type", "application/json")
                .header("Idempotency-Key", orderId)
                .timeout(timeout)
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();

        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            int status = response.statusCode();
            if (status == 200 || status == 201) {
                return ChargeResult.CHARGED;
            } else if (status == 402) {
                return ChargeResult.DECLINED;
            } else {
                return ChargeResult.SERVER_ERROR;
            }
        } catch (InterruptedException e) {
            // Real interrupt (worker shutdown) — preserve the flag.
            Thread.currentThread().interrupt();
            return ChargeResult.NETWORK;
        } catch (java.io.IOException e) {
            // Timeout or connection drop. Do NOT set the interrupt flag —
            // it would break subsequent blocking calls (hasCharges, sleep)
            // on this worker thread.
            return ChargeResult.NETWORK;
        }
    }

    public boolean hasCharges(String orderId, Duration timeout) {
        String encoded = URLEncoder.encode(orderId, StandardCharsets.UTF_8);
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/payments?order_id=" + encoded))
                .timeout(timeout)
                .GET()
                .build();

        try {
            HttpResponse<String> response = lookupClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) {
                return false;
            }
            JsonNode node = objectMapper.readTree(response.body());
            return node.isArray() && node.size() > 0;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        } catch (Exception e) {
            return false;
        }
    }

    public enum ChargeResult {
        CHARGED, DECLINED, SERVER_ERROR, NETWORK
    }
}
