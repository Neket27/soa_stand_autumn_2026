package orders.api.dto;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;

/**
 * Товар в заказе.
 *
 * SKU — идентификатор товара (Keeping Unit)
 * qty — количество
 * price_cents — цена в копейках
 */
public record Item(
        @NotBlank(message = "sku обязателен")
        String sku,
        @Min(value = 1, message = "qty должен быть >= 1")
        int qty,
        @Min(value = 0, message = "price_cents не может быть отрицательным")
        long priceCents
) {}
