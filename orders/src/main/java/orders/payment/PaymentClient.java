package orders.payment;

import com.fasterxml.jackson.databind.ObjectMapper;
import orders.api.dto.PaymentChargeDto;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.converter.HttpMessageConverter;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.stereotype.Component;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.RestTemplate;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import orders.store.OrderRepository;

import java.time.Duration;

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
 * === ПОВТОРЫ И ИДЕМПОТЕНТНОСТЬ (уже реализовано) ===
 *
 * 402 ловится отдельно через HttpStatusCodeException: RestTemplate
 * БРОСКАЕТ исключение на 4xx, и без отдельной ветки 402 превратился бы
 * в NETWORK, а заказ завис бы в PENDING.
 *
 * Повтор charge() с тем же Idempotency-Key возвращает Payment
 * сохранённый результат без нового списания — на это опираются retry-и
 * Consumer'а (500/сеть) и Reaper.
 */
@Component
public class PaymentClient {

    private final RestTemplate restTemplate;
    private final OrderRepository orderRepository;

    @Value("${PAYMENT_URL}")
    private String paymentUrl;

    public PaymentClient(OrderRepository orderRepository, ObjectMapper objectMapper) {
        this.orderRepository = orderRepository;
        // Таймауты: без них RestTemplate ждёт ответ бесконечно,
        // и при "зависшем" Payment навсегда зависнет Consumer/Reaper.
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(5));
        factory.setReadTimeout(Duration.ofSeconds(10));
        RestTemplate restTemplate = new RestTemplate(factory);
        // Подставляем контекстный ObjectMapper: только он знает snake_case-
        // стратегию (JacksonConfig). Без неё DTO ушёл бы в JSON как
        // {"orderId":...}, а Payment ждёт {"order_id":...,"amount_cents":...}
        // — и отвечал бы 400 на каждый запрос (так и было: 2000+ ошибок).
        for (HttpMessageConverter<?> converter : restTemplate.getMessageConverters()) {
            if (converter instanceof MappingJackson2HttpMessageConverter jackson) {
                jackson.setObjectMapper(objectMapper);
            }
        }
        this.restTemplate = restTemplate;
    }

    /**
     * Попытка оплаты заказа.
     *
     * Сумма берётся из БД заказа (amount_cents = Σ qty × price_cents):
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

        PaymentChargeDto paymentChargeDto = new PaymentChargeDto(orderId, amountCents,"RUB");

        try {
            restTemplate.postForEntity(
                    paymentUrl + "/payments",
                    new HttpEntity<>(paymentChargeDto, headers),
                    String.class
            );
            // Только 2xx не бросают исключение
            return new PaymentResult(PaymentStatus.CHARGED, null);
        } catch (HttpStatusCodeException e) {
            // Ответ получен, но с ошибочным кодом.
            // 402 → DECLINED (банк отклонил, резерв нужно снять)
            // 5xx и прочее → SERVER_ERROR (retry)
            int code = e.getStatusCode().value();
            if (code == 402)
                return new PaymentResult(PaymentStatus.DECLINED, "банк отклонил оплату");

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
