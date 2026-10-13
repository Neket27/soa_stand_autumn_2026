package orders.api.dto;

/**
 * Стандартный формат ошибки API.
 *
 * Пример JSON:
 * {"error": "not_found", "message": "заказ не найден"}
 */
public record ApiError(
        String error,
        String message
) {}
