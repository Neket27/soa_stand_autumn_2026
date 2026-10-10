package orders.inventory;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import lombok.extern.slf4j.Slf4j;
import orders.api.dto.Item;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;

@Slf4j
@Service
public class InventoryClient {

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

    public boolean reserve(String orderId, List<Item> items, Duration timeout) {
        String body;
        try {
            ObjectNode root = objectMapper.createObjectNode();
            root.put("order_id", orderId);
            ArrayNode itemsNode = objectMapper.createArrayNode();
            for (Item i : items) {
                ObjectNode itemNode = objectMapper.createObjectNode();
                itemNode.put("sku", i.sku());
                itemNode.put("qty", i.qty());
                itemsNode.add(itemNode);
            }
            root.set("items", itemsNode);
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
            log.warn("Request interrupted");
            return false;
        } catch (IOException e) {
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
            log.warn("Request interrupted");
            return false;
        } catch (IOException e) {
            return false;
        }
    }
}
