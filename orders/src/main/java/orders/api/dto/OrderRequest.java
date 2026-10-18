package orders.api.dto;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;

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
 *
 * Валидация декларативная (Bean Validation):
 * — проверки описаны прямо в DTO аннотациями;
 * — @Valid у контроллера запускает их (включая вложенные Item);
 * — нарушения → MethodArgumentNotValidException → 400 из GlobalExceptionHandler.
 */
public record OrderRequest(
        @NotBlank(message = "user_id обязателен")
        String userId,
        @NotEmpty(message = "items не может быть пустым")
        @Valid
        List<Item> items
) {}
