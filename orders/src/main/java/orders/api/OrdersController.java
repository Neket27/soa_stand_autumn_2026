package orders.api;

import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import orders.api.dto.Order;
import orders.api.dto.OrderCreated;
import orders.api.dto.OrderRequest;
import orders.service.CreateOrderService;
import orders.service.OrderQueryService;

/**
 * REST-контроллер для заказов.
 *
 * Тонкий адаптер: разбирает HTTP-запрос, делегирует сервисам и
 * отдаёт ответ. Бизнес-логики здесь нет (см. комментарии в
 * OrderQueryService — зачем нужен сервисный слой).
 *
 * === ЭНДПОИНТЫ ===
 * POST /orders    — создать заказ (CreateOrderService)
 * GET  /orders/{id} — получить заказ (OrderQueryService)
 *
 * === КОНТРАКТ ОТВЕТА ===
 * POST /orders всегда отвечает 201 + {"id": ..., "status": "pending"}:
 * оплата асинхронна (см. CreateOrderService), итоговый статус — через
 * GET /orders/{id}. Ответ принципиально не ждёт Payment, поэтому бюджет
 * 1 с на POST держится и при задержке ответа Payment (S3, 2 с), и при
 * его падении (S4). 5xx с этого эндпоинта — только при реальных сбоях
 * самого Orders (Inventory, БД).
 */
@RestController
@RequestMapping("/orders")
@RequiredArgsConstructor
public class OrdersController {

    private final CreateOrderService createOrderService;
    private final OrderQueryService orderQueryService;

    @PostMapping
    public ResponseEntity<OrderCreated> create(@Valid @RequestBody OrderRequest request) {
        // @Valid запускает Bean Validation на OrderRequest/Item ДО входа в сервис:
        // нарушения → MethodArgumentNotValidException → 400 в GlobalExceptionHandler.
        OrderCreated created = createOrderService.create(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(created);
    }

    @GetMapping("/{id}")
    public ResponseEntity<Order> get(@PathVariable String id) {
        // Отсутствие заказа — OrderNotFoundException из сервиса,
        // его ловит GlobalExceptionHandler и отдаёт 404.
        return ResponseEntity.ok(orderQueryService.getOrder(id));
    }
}
