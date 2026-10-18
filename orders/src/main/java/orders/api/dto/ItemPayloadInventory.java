package orders.api.dto;

/**
 * Позиция резерва в запросе к Inventory.
 * Поле JSON — "sku" (контракт с заглушкой Inventory).
 */
public record ItemPayloadInventory(String sku, int qty) {
}
