package orders.api;

import lombok.RequiredArgsConstructor;
import orders.api.dto.Order;
import orders.store.OrderRepository;
import org.springframework.stereotype.Service;

/**
 * Read use case for GET /orders/{id}: owns the lookup, the mapping to the
 * API DTO and the "order does not exist" decision.
 */
@Service
@RequiredArgsConstructor
public class OrderQueryService {

    private final OrderRepository orderRepository;

    public Order getOrder(String id) {
        OrderRepository.StoredOrder order = orderRepository.findById(id);
        if (order == null) {
            throw new OrderNotFoundException(id);
        }
        return new Order(order.id(), order.status(), order.amountCents());
    }
}
