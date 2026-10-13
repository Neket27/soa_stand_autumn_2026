package orders.api.dto;

/**
 * Ответ на получение заказа.
 *
 * Пример JSON:
 * {"id": "550e8400-e29b-41d4-a716-446655440000", "status": "paid", "amount_cents": 1000}
 */
public record Order(
        String id,
        String status,
        long amountCents
) {}
