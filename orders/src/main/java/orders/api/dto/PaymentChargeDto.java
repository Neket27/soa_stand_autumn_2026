package orders.api.dto;

/**
 * Тело POST {PAYMENT_URL}/payments:
 * {"order_id": "...", "amount_cents": 1000, "currency": "RUB"}
 *
 * orderId/amountCents уходят в JSON как "order_id"/"amount_cents"
 * благодаря snake_case-стратегии из JacksonConfig — поэтому
 * RestTemplate должен работать с тем же ObjectMapper (см. PaymentClient).
 *
 * Сумма — строго amount_cents заказа из БД (Σ qty × price_cents):
 * заглушка Payment проверяет «списана сумма заказа».
 */
public record PaymentChargeDto(String orderId, long amountCents, String currency) {
}
