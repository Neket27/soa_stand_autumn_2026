package orders.api.dto;

import java.util.List;

/**
 * Тело запроса на создание заказа.
 *
 * Пример JSON:
 * {
 *   "user_id": "user-123",
 *   "items": [
 *     {"sku": "SKU-001", "qty": 2, "price_cents": 500}
 *   ]
 * }
 */
public record OrderRequest(
        String userId,
        List<Item> items
) {}
