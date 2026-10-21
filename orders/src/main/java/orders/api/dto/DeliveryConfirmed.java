package orders.api.dto;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;

/**
 * Входящее событие delivery.confirmed от шины (contracts/events.yaml).
 *
 * Служба доставки присылает это событие в POST /webhooks/delivery.
 * ВАЖНО: событие не ссылается на заказы — начисление идёт строго по
 * user_id из события, без поиска заказа (требование контракта).
 */
public record DeliveryConfirmed(
        @NotBlank(message = "id обязателен")
        String id,
        @NotBlank(message = "type обязателен")
        @Pattern(regexp = DeliveryConfirmed.TYPE, message = "неизвестный тип события")
        String type,
        @NotBlank(message = "user_id обязателен")
        String userId,
        @Min(value = 1, message = "bonus_points должен быть не меньше 1")
        long bonusPoints,
        String occurredAt
) {

    public static final String TYPE = "delivery.confirmed";
}
