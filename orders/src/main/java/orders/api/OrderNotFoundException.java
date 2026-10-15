package orders.api;

/**
 * Заказ не найден.
 *
 * Доменное исключение: сервисное слой бросает его, не зная про HTTP.
 * GlobalExceptionHandler переводит в ответ 404 {"error": "not_found", ...}.
 */
public class OrderNotFoundException extends RuntimeException {

    public OrderNotFoundException(String orderId) {
        super("заказ не найден: " + orderId);
    }
}
