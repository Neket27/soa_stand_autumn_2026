package orders.api;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.validation.Valid;
import orders.api.dto.Error;
import orders.api.dto.Item;
import orders.api.dto.Order;
import orders.api.dto.OrderCreated;
import orders.api.dto.OrderRequest;
import orders.flow.BackgroundWorker;
import orders.flow.OrderProcessor;
import orders.store.OrderRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.time.Duration;
import java.util.List;

@RestController
public class OrdersController {

    private final OrderRepository orderRepository;
    private final OrderProcessor orderProcessor;
    private final BackgroundWorker backgroundWorker;
    private final ObjectMapper objectMapper;
    private final long budgetMs;

    public OrdersController(OrderRepository orderRepository,
                            OrderProcessor orderProcessor,
                            BackgroundWorker backgroundWorker,
                            ObjectMapper objectMapper,
                            @Value("${orders.process.budget-ms:900}") long budgetMs) {
        this.orderRepository = orderRepository;
        this.orderProcessor = orderProcessor;
        this.backgroundWorker = backgroundWorker;
        this.objectMapper = objectMapper;
        this.budgetMs = budgetMs;
    }

    @GetMapping("/healthz")
    public ResponseEntity<Void> healthz() {
        return ResponseEntity.ok().build();
    }

    @PostMapping("/orders")
    public ResponseEntity<?> createOrder(@Valid @RequestBody OrderRequest request) {
        long amountCents = 0;
        for (Item item : request.items()) {
            amountCents += (long) item.qty() * item.priceCents();
        }

        String itemsJson = buildItemsJson(request.items());

        String orderId = orderRepository.insertPending(request.user_id(), request.items(), amountCents);

        Duration budget = Duration.ofMillis(budgetMs);
        String result = orderProcessor.processOnce(orderId, amountCents, itemsJson, budget);

        // processOnce returns only REJECTED (reserve not taken) or PENDING
        // (reserve taken, outcome decided by the background worker).
        if ("REJECTED".equals(result)) {
            orderRepository.transition(orderId, "rejected");
            return ResponseEntity.status(201).body(new OrderCreated(orderId, "rejected"));
        }
        backgroundWorker.enqueue(orderId);
        return ResponseEntity.status(201).body(new OrderCreated(orderId, "pending"));
    }

    @GetMapping("/orders/{id}")
    public ResponseEntity<Order> getOrder(@PathVariable String id) {
        OrderRepository.Order order = orderRepository.findById(id);
        if (order == null) {
            return ResponseEntity.notFound().build();
        }
        return ResponseEntity.ok(new Order(order.id(), order.status(), order.amountCents()));
    }

    private String buildItemsJson(List<Item> items) {
        try {
            return objectMapper.writeValueAsString(items);
        } catch (JsonProcessingException e) {
            throw new RuntimeException("failed to serialize items", e);
        }
    }
}
