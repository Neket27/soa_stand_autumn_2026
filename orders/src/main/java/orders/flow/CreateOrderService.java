package orders.flow;

import orders.api.dto.OrderRequest;
import orders.inventory.InventoryClient;
import orders.store.OrderRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;
import java.time.Duration;

/**
 * Синхронный use case для POST /orders: вставить pending, зарезервировать товар
 * в пределах бюджета ответа, затем либо reject (резерв не удался), либо
 * поставить в очередь на фоновое списание.
 */
@Service
    public class CreateOrderService {

    private final OrderRepository orderRepository;
    private final InventoryClient inventoryClient;
    private final InMemoryChargeDispatcher chargeDispatcher;
    private final long budgetMs;

    public CreateOrderService(OrderRepository orderRepository,
                              InventoryClient inventoryClient,
                              InMemoryChargeDispatcher chargeDispatcher,
                              @Value("${orders.process.budget-ms:900}") long budgetMs) {
        this.orderRepository = orderRepository;
        this.inventoryClient = inventoryClient;
        this.chargeDispatcher = chargeDispatcher;
        this.budgetMs = budgetMs;
    }

    public CreateResult createOrder(OrderRequest request) {
        BigDecimal amountCents = request.amountCents();
        String orderId = orderRepository.insertPending(request.userId(), request.items(), amountCents);

        Duration reserveTimeout = Duration.ofMillis(Math.max(100, budgetMs - 10));
        boolean reserved = inventoryClient.reserve(orderId, request.items(), reserveTimeout);
        if (!reserved) {
            // Неопределённый исход: тайм-аут — это не «резерв не создан», он мог
            // создаться. Reject (в рамках бюджета, без лишнего вызова Inventory на этом пути)
            // и записать durable release duty; release-sweep в reaper ретраит
            // DELETE (идемпотентный, 204), пока резерв не подтверждён снятым.
            orderRepository.transition(orderId, OrderStatus.REJECTED, true);
            return new CreateResult(orderId, OrderStatus.REJECTED);
        }

        chargeDispatcher.enqueue(orderId);
        return new CreateResult(orderId, OrderStatus.PENDING);
    }

    public record CreateResult(String orderId, OrderStatus status) {}
}
