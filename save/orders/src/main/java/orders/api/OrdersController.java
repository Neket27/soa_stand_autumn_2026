package orders.api;

import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import orders.api.dto.Order;
import orders.api.dto.OrderCreated;
import orders.api.dto.OrderRequest;
import orders.flow.CreateOrderService;
import orders.store.OrderRepository;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequiredArgsConstructor
public class OrdersController {

    private final OrderRepository orderRepository;
    private final CreateOrderService createOrderService;
    private final OrderQueryService orderQueryService;

    @GetMapping("/healthz")
    public ResponseEntity<Void> healthz() {
        return orderRepository.isReady()
                ? ResponseEntity.ok().build()
                : ResponseEntity.status(503).build();
    }

    @PostMapping("/orders")
    @ResponseStatus(HttpStatus.CREATED)
    public OrderCreated createOrder(@Valid @RequestBody OrderRequest request) {
        CreateOrderService.CreateResult result = createOrderService.createOrder(request);
        return new OrderCreated(result.orderId(), result.status().name().toLowerCase());
    }

    @GetMapping("/orders/{id}")
    public Order getOrder(@PathVariable String id) {
        return orderQueryService.getOrder(id);
    }
}
