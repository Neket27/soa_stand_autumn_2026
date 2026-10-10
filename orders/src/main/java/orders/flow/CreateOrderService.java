package orders.flow;

import orders.api.dto.OrderRequest;
import orders.inventory.InventoryClient;
import orders.store.OrderRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;
import java.time.Duration;

/**
 * Synchronous use case for POST /orders: insert pending, reserve stock within
 * the response budget, then either reject (reserve failed) or enqueue for
 * background charge.
 */
@Service
public class CreateOrderService {

    private final OrderRepository orderRepository;
    private final InventoryClient inventoryClient;
    private final BackgroundWorker backgroundWorker;
    private final long budgetMs;

    public CreateOrderService(OrderRepository orderRepository,
                              InventoryClient inventoryClient,
                              BackgroundWorker backgroundWorker,
                              @Value("${orders.process.budget-ms:900}") long budgetMs) {
        this.orderRepository = orderRepository;
        this.inventoryClient = inventoryClient;
        this.backgroundWorker = backgroundWorker;
        this.budgetMs = budgetMs;
    }

    public CreateResult createOrder(OrderRequest request) {
        BigDecimal amountCents = request.amountCents();
        String orderId = orderRepository.insertPending(request.userId(), request.items(), amountCents);

        Duration reserveTimeout = Duration.ofMillis(Math.max(100, budgetMs - 10));
        boolean reserved = inventoryClient.reserve(orderId, request.items(), reserveTimeout);
        if (!reserved) {
            orderRepository.transition(orderId, OrderStatus.REJECTED);
            return new CreateResult(orderId, OrderStatus.REJECTED);
        }

        backgroundWorker.enqueue(orderId);
        return new CreateResult(orderId, OrderStatus.PENDING);
    }

    public record CreateResult(String orderId, OrderStatus status) {}
}
