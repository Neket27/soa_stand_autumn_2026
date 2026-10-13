package orders.api;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import orders.api.dto.Order;
import orders.api.dto.OrderCreated;
import orders.api.dto.OrderRequest;
import orders.flow.CreateOrderService;
import orders.store.OrderRepository;

import java.util.Map;

/**
 * REST-контроллер для заказов.
 *
 * === ЭНДПОИНТЫ ===
 * POST /orders — создать заказ
 * GET /orders/{id} — получить заказ
 * GET /healthz — проверка здоровья
 *
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 *
 * S1: POST /orders должен вернуть:
 *     — 201 + {"id": "...", "status": "paid"} если оплачен
 *     — 201 + {"id": "...", "status": "rejected"} если отклонён банком
 *
 * S2: POST /orders при 500 от Payment:
 *     — Должен вернуть 201 + {"id": "...", "status": "pending"}
 *     — Или 201 + {"id": "...", "status": "paid"} если retry прошёл быстро
 *     — Главное: НЕ 500!
 */
@RestController
@RequestMapping("/orders")
public class OrdersController {

    private final CreateOrderService createOrderService;
    private final OrderRepository orderRepository;

    public OrdersController(CreateOrderService createOrderService,
                            OrderRepository orderRepository) {
        this.createOrderService = createOrderService;
        this.orderRepository = orderRepository;
    }

    @PostMapping
    public ResponseEntity<OrderCreated> create(@RequestBody OrderRequest request) {
        OrderCreated created = createOrderService.create(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(created);
    }

    @GetMapping("/{id}")
    public ResponseEntity<?> get(@PathVariable String id) {
        Order order = orderRepository.findById(id);
        if (order == null) {
            return ResponseEntity.status(HttpStatus.NOT_FOUND)
                    .body(Map.of("error", "not_found", "message", "заказ не найден"));
        }
        return ResponseEntity.ok(order);
    }

    @GetMapping("/healthz")
    public ResponseEntity<Map<String, String>> health() {
        try {
            orderRepository.isReady();
            return ResponseEntity.ok(Map.of("status", "ok"));
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(Map.of("status", "unavailable"));
        }
    }
}
