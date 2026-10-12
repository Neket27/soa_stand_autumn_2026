package orders.api;

import lombok.Getter;

/** Идентификатор заказа, который Orders никогда не принимал (нет строки в хранилище). */
@Getter
public class OrderNotFoundException extends RuntimeException {

    private final String orderId;

    public OrderNotFoundException(String orderId) {
        super("order not found: " + orderId);
        this.orderId = orderId;
    }

}
