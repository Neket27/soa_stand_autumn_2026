package orders.payment;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.math.BigDecimal;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

@Slf4j
@Service
public class PaymentClient {

    private final HttpClient httpClient;
    private final String baseUrl;
    private final ObjectMapper objectMapper;

    public PaymentClient(
            @Value("${PAYMENT_URL}") String baseUrl,
            @Value("${PAYMENT.CONNECT_TIMEOUT_MS:200}") int connectTimeoutMs,
            ObjectMapper objectMapper) {
        this.baseUrl = baseUrl;
        this.objectMapper = objectMapper;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofMillis(connectTimeoutMs))
                .build();
    }

    public ChargeResult charge(String orderId, BigDecimal amountCents, Duration timeout) {
        String body;
        try {
            body = objectMapper.writeValueAsString(
                    new ChargeRequest(orderId, amountCents, "RUB"));
        } catch (Exception e) {
            throw new RuntimeException("failed to serialize charge request", e);
        }

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
            Thread.currentThread().interrupt();
            return ChargeResult.NETWORK;
        } catch (IOException e) {
            log.warn("charge error: order={} ({})", orderId, e.getClass().getSimpleName());
            return ChargeResult.NETWORK;
        }
    }

    /**
     * Three states: a failed lookup must NOT be reported as "no charge" —
     * "verified empty" and "unable to verify" lead to different decisions
     * (reject vs stay pending).
     */
    public enum ReconciliationResult {
        CHARGED, NOT_CHARGED, UNKNOWN
    }

    public ReconciliationResult reconcile(String orderId, Duration timeout) {
        String encoded = URLEncoder.encode(orderId, StandardCharsets.UTF_8);
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/payments?order_id=" + encoded))
                .timeout(timeout)
                .GET()
                .build();

        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) {
                log.warn("reconcile non-200: order={} status={}", orderId, response.statusCode());
                return ReconciliationResult.UNKNOWN;
            }
            JsonNode node = objectMapper.readTree(response.body());
            if (!node.isArray()) {
                log.warn("reconcile unexpected body: order={}", orderId);
                return ReconciliationResult.UNKNOWN;
            }
            return !node.isEmpty() ? ReconciliationResult.CHARGED : ReconciliationResult.NOT_CHARGED;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            log.warn("reconcile interrupted: order={}", orderId);
            return ReconciliationResult.UNKNOWN;
        } catch (Exception e) {
            log.warn("reconcile error: order={} ({})", orderId, e.getClass().getSimpleName());
            return ReconciliationResult.UNKNOWN;
        }
    }

    public enum ChargeResult {
        CHARGED, DECLINED, SERVER_ERROR, NETWORK
    }

    record ChargeRequest(
            @JsonProperty("order_id") String orderId,
            @JsonProperty("amount_cents") BigDecimal amountCents,
            @JsonProperty("currency") String currency) {}
}
