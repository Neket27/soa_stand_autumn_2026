package orders.api.dto;

import java.util.List;

/**
 * Тело POST {INVENTORY_URL}/reservations:
 * {"order_id": "...", "items": [{"sku": "...", "qty": 1}]}
 *
 * orderId уходит в JSON как "order_id" благодаря snake_case-стратегии
 * из JacksonConfig — поэтому RestTemplate должен работать с тем же
 * ObjectMapper (см. InventoryClient).
 */
public record InventoryReserveItemsDto(String orderId, List<ItemPayloadInventory> items) {
}
