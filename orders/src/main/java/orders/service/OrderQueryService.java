package orders.service;

import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import orders.api.OrderNotFoundException;
import orders.api.dto.Order;
import orders.store.OrderRepository;

/**
 * Сервис чтения заказов (use case: get order).
 */
@Service
@RequiredArgsConstructor
public class OrderQueryService {

    private final OrderRepository orderRepository;

    public Order getOrder(String orderId) {
        Order order = orderRepository.findById(orderId);
        if (order == null)
            throw new OrderNotFoundException(orderId);
        return order;
    }
}
