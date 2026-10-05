package orders.inventory;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

public class InventoryClient {

    private final HttpClient httpClient;
    private final String baseUrl;

    public InventoryClient(String baseUrl, int timeoutMs) {
        this.baseUrl = baseUrl;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(timeoutMs))
                .build();
    }

    public boolean reserve(String orderId, String itemsJson, Duration timeout) {
        String body = String.format(
                "{\"order_id\":\"%s\",\"items\":%s}",
                orderId, itemsJson);

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
