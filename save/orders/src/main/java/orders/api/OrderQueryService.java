package orders.api;

import lombok.RequiredArgsConstructor;
import orders.api.dto.Order;
import orders.store.OrderRepository;
import org.springframework.stereotype.Service;

/**
 * Read use case для GET /orders/{id}: владеет поиском, маппингом в API-DTO
 * и решением «заказ не существует».
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
        return new Order(order.id(), order.status().name().toLowerCase(), order.amountCents());
    }
}
