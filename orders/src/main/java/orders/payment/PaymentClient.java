package orders.payment;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;

import java.util.Map;

/**
 * HTTP-клиент для Payment API.
 *
 * === КАК РАБОТАЕТ ===
 * POST {PAYMENT_URL}/payments с телом:
 * {
 *   "order_id": "...",
 *   "amount_cents": 1000,
 *   "currency": "RUB",
 *   "idempotency-key": "..."
 * }
 *
 * === КОДЫ ОТВЕТА ===
 * 201 — платёж успешен (CHARGED)
 * 402 — банк отклонил (DECLINED) — не ошибка, просто отказ
 * 500 — внутренняя ошибка Payment (SERVER_ERROR) — нужно retry
 * Сетевая ошибка — Payment недоступен (NETWORK) — нужно retry
 *
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 *
 * S1: Payment возвращает 402 на 10% запросов.
 *     — Здесь уже всё обработано: 402 → DECLINED
 *     — Проверь, что 402 не вызывает exception
 *
 * S2: Payment возвращает 500 на 33% запросов.
 *     — Здесь уже всё обработано: 500 → SERVER_ERROR
 *     — Но нужно убедиться, что Consumer делает retry
 */
@Component
public class PaymentClient {

    private final RestTemplate restTemplate = new RestTemplate();

    @Value("${PAYMENT_URL}")
    private String paymentUrl;

    /**
     * Попытка оплаты заказа.
     *
     * === ЧТО СДЕЛАТЬ САМОМУ (S2) ===
     *
     * Sейчас при 500 возвращается SERVER_ERROR, и Consumer делает NAK.
     * Но NAK без задержки может привести к бесконечному циклу.
     *
     * Вариант решения:
     * 1. Добавить Idempotency-Key (уже есть в коде)
     * 2. Добавить backoff в Consumer перед NAK
     * 3. Использовать Dead Letter Topic для проблемных событий
     */
    public PaymentResult charge(String orderId) {
        String idempotencyKey = "pay-" + orderId;

        Map<String, Object> body = Map.of(
                "order_id", orderId,
                "amount_cents", getAmountForOrder(orderId), // нужно передавать реальную сумму
                "currency", "RUB",
                "idempotency_key", idempotencyKey
        );

        try {
            var response = restTemplate.postForEntity(
                    paymentUrl + "/payments",
                    body,
                    Map.class
            );

            return switch (response.getStatusCode().value()) {
                case 200, 201 -> new PaymentResult(PaymentStatus.CHARGE, null);
                case 402 -> new PaymentResult(PaymentStatus.DECLINED, "банк отклонил оплату");
                default -> new PaymentResult(PaymentStatus.SERVER_ERROR, "неожиданный код: " + response.getStatusCode());
            };
        } catch (org.springframework.web.client.HttpServerErrorException e) {
            // 500 от Payment
            return new PaymentResult(PaymentStatus.SERVER_ERROR, e.getMessage());
        } catch (Exception e) {
            // Сетевая ошибка: Payment недоступен
            return new PaymentResult(PaymentStatus.NETWORK, e.getMessage());
        }
    }

    private long getAmountForOrder(String orderId) {
        // TODO: передавать реальную сумму заказа
        // Сейчас заглушка — нужно передавать amount_cents из заказа
        return 1000L;
    }

    public enum PaymentStatus {
        CHARGED,    // Оплачено
        DECLINED,   // Отклонено банком (402)
        SERVER_ERROR, // Ошибка сервера (500)
        NETWORK     // Сетевая ошибка
    }

    public record PaymentResult(PaymentStatus status, String message) {}
}
