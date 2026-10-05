package orders.api;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import orders.api.dto.Error;
import orders.api.dto.Item;
import orders.api.dto.Order;
import orders.api.dto.OrderCreated;
import orders.api.dto.OrderRequest;
import orders.flow.BackgroundWorker;
import orders.flow.OrderProcessor;
import orders.store.OrderRepository;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.time.Duration;
import java.util.List;

@RestController
public class OrdersController {

    private static final ObjectMapper objectMapper = new ObjectMapper();

    private final OrderRepository orderRepository;
    private final OrderProcessor orderProcessor;
    private final BackgroundWorker backgroundWorker;

    public OrdersController(OrderRepository orderRepository,
                            OrderProcessor orderProcessor,
                            BackgroundWorker backgroundWorker) {
        this.orderRepository = orderRepository;
        this.orderProcessor = orderProcessor;
        this.backgroundWorker = backgroundWorker;
    }

    @GetMapping("/healthz")
    public ResponseEntity<Void> healthz() {
        return ResponseEntity.ok().build();
    }

    @PostMapping("/orders")
    public ResponseEntity<?> createOrder(@RequestBody OrderRequest request) {
        if (!valid(request)) {
            return ResponseEntity.badRequest().body(new Error("validation_error", "invalid request body"));
        }

        long amountCents = 0;
        for (Item item : request.items()) {
            amountCents += (long) item.qty() * item.priceCents();
        }

        String itemsJson = buildItemsJson(request.items());

        String orderId = orderRepository.insertPending(request.user_id(), request.items(), amountCents);

        Duration budget = Duration.ofMillis(900);
        String result = orderProcessor.processOnce(orderId, amountCents, itemsJson, budget);

        if ("PAID".equals(result)) {
            orderRepository.transition(orderId, "paid");
            return ResponseEntity.status(201).body(new OrderCreated(orderId, "paid"));
        }
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

    private boolean valid(OrderRequest req) {
        if (req.user_id() == null || req.user_id().isEmpty()) return false;
        if (req.items() == null || req.items().isEmpty()) return false;
        for (Item item : req.items()) {
            if (item.sku() == null || item.sku().isEmpty()) return false;
            if (item.qty() < 1) return false;
            if (item.priceCents() < 0) return false;
        }
        return true;
    }

    private String buildItemsJson(List<Item> items) {
        try {
            return objectMapper.writeValueAsString(items);
        } catch (JsonProcessingException e) {
            throw new RuntimeException("failed to serialize items", e);
        }
    }
}
