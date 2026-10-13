package orders.api;

/**
 * Исключение, когда заказ не найден.
 * Выбрасывается в OrderQueryService, когда GET /orders/{id} не находит заказ.
 */
public class OrderNotFoundException extends RuntimeException {
    public OrderNotFoundException(String orderId) {
        super("Заказ не найден: " + orderId);
    }
}
