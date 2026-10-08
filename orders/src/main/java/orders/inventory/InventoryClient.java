package orders.inventory;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

@Service
public class InventoryClient {

    private static final Logger log = LoggerFactory.getLogger(InventoryClient.class);

    private final HttpClient httpClient;
    private final String baseUrl;
    private final ObjectMapper objectMapper;

    public InventoryClient(
            @Value("${INVENTORY_URL}") String baseUrl,
            @Value("${INVENTORY.TIMEOUT_MS:200}") int timeoutMs,
            ObjectMapper objectMapper) {
        this.baseUrl = baseUrl;
        this.objectMapper = objectMapper;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(timeoutMs))
                .build();
    }

    public boolean reserve(String orderId, String itemsJson, Duration timeout) {
        String body;
        try {
            ObjectNode root = objectMapper.createObjectNode();
            root.put("order_id", orderId);
            root.set("items", objectMapper.readTree(itemsJson));
            body = objectMapper.writeValueAsString(root);
        } catch (Exception e) {
            throw new RuntimeException("failed to serialize reserve request", e);
        }

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/reservations"))
                .header("Content-Type", "application/json")
                .timeout(timeout)
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();

        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            int status = response.statusCode();
            return status == 200 || status == 201;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        } catch (java.io.IOException e) {
            log.warn("reserve error: order={}", orderId);
            return false;
        }
    }

    public boolean release(String orderId, Duration timeout) {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/reservations/" + orderId))
                .timeout(timeout)
                .DELETE()
                .build();

        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            int status = response.statusCode();
            return status == 204;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        } catch (java.io.IOException e) {
            return false;
        }
    }
}
