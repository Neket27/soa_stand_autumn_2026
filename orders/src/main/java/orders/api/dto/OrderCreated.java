package orders.api.dto;

/**
 * Ответ на создание заказа.
 *
 * Пример JSON:
 * {"id": "550e8400-e29b-41d4-a716-446655440000", "status": "paid"}
 */
public record OrderCreated(
        String id,
        String status
) {}
