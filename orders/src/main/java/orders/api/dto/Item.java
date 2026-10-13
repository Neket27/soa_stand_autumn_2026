package orders.api.dto;

/**
 * Товар в заказе.
 *
 * SKU — идентификатор товара (Keeping Unit)
 * qty — количество
 * price_cents — цена в копейках
 */
public record Item(
        String sku,
        int qty,
        long priceCents
) {}
