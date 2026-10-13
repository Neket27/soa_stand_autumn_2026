package orders.payment;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.RestTemplate;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import orders.store.OrderRepository;

import java.time.Duration;
import java.util.Map;

/**
 * HTTP-клиент для Payment API.
 *
 * === КАК РАБОТАЕТ ===
 * POST {PAYMENT_URL}/payments с телом:
 * {
 *   "order_id": "...",
 *   "amount_cents": 1000,
 *   "currency": "RUB"
 * }
 * и заголовком Idempotency-Key: pay-{orderId}
 *
 * === КОДЫ ОТВЕТА ===
 * 201/200 — платёж успешен (CHARGED)
 * 402     — банк отклонил (DECLINED) — не ошибка, просто отказ
 * 500     — внутренняя ошибка Payment (SERVER_ERROR) — нужно retry
 * Сетевая ошибка / таймаут — Payment недоступен (NETWORK) — нужно retry
 *
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 *
 * S1: Payment возвращает 402 на 10% запросов.
 *     — ВАЖНО: RestTemplate на 4xx БРОСКАЕТ исключение
 *       (HttpClientErrorException), а не возвращает ответ.
 *       Поэтому 402 ловим через HttpStatusCodeException и
 *       проверяем код == 402. Если просто ловить Exception,
 *       402 превратится в NETWORK и заказ зависнет в PENDING.
 *
 * S2: Payment возвращает 500 на 33% запросов.
 *     — 5xx превращаем в SERVER_ERROR → Consumer делает retry
 *     — Idempotency-Key в ЗАГОЛОВКЕ (не в теле!) защищает от
 *       двойного списания при retry: повтор с тем же ключом
 *       вернёт сохранённый результат без нового списания
 */
@Component
public class PaymentClient {

    private final RestTemplate restTemplate;
    private final OrderRepository orderRepository;

    @Value("${PAYMENT_URL}")
    private String paymentUrl;

    public PaymentClient(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
        // Таймауты: без них RestTemplate ждёт ответ бесконечно,
        // и при "зависшем" Payment навсегда зависнет Consumer/Reaper.
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(5));
        factory.setReadTimeout(Duration.ofSeconds(10));
        this.restTemplate = new RestTemplate(factory);
    }

    /**
     * Попытка оплаты заказа.
     *
     * Сумма берётся из БД заказа (amount_cents = Σ qty × price_cents):
     * заглушка "1000" не пройдёт проверку "списана сумма заказа".
     */
    public PaymentResult charge(String orderId) {
        long amountCents = orderRepository.getAmount(orderId);
        String idempotencyKey = "pay-" + orderId;

        // Ключ идемпотентности — в ЗАГОЛОВКЕ: так его читает заглушка
        // (r.Header.Get("Idempotency-Key")). Повторная отправка того же
        // заказа не создаёт второе списание.
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        headers.set("Idempotency-Key", idempotencyKey);

        Map<String, Object> body = Map.of(
                "order_id", orderId,
                "amount_cents", amountCents,
                "currency", "RUB"
        );

        try {
            restTemplate.postForEntity(
                    paymentUrl + "/payments",
                    new HttpEntity<>(body, headers),
                    String.class
            );
            // Только 2xx не бросают исключение
            return new PaymentResult(PaymentStatus.CHARGED, null);
        } catch (HttpStatusCodeException e) {
            // Ответ получен, но с ошибочным кодом.
            // 402 → DECLINED (банк отклонил, резерв нужно снять)
            // 5xx и прочее → SERVER_ERROR (retry)
            int code = e.getStatusCode().value();
            if (code == 402) {
                return new PaymentResult(PaymentStatus.DECLINED, "банк отклонил оплату");
            }
            return new PaymentResult(PaymentStatus.SERVER_ERROR, "HTTP " + code);
        } catch (Exception e) {
            // Сетевая ошибка: Payment недоступен или таймаут
            return new PaymentResult(PaymentStatus.NETWORK, e.getMessage());
        }
    }

    public enum PaymentStatus {
        CHARGED,      // Оплачено
        DECLINED,     // Отклонено банком (402)
        SERVER_ERROR, // Ошибка сервера (500)
        NETWORK       // Сетевая ошибка / таймаут
    }

    public record PaymentResult(PaymentStatus status, String message) {}
}
