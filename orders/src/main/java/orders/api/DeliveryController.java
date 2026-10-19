package orders.api;

import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import orders.api.dto.BonusBalance;
import orders.api.dto.DeliveryConfirmed;
import orders.service.BonusService;

/**
 * REST-контроллер шины доставки и бонусов (задание 2).
 *
 * === ЭНДПОИНТЫ (contracts/orders.yaml) ===
 *
 * POST /webhooks/delivery — шина сюда доставляет delivery.confirmed.
 *     Отвечаем 2xx ВСЕГДА, включая повторно доставленные события:
 *     шина повторяет доставку раз в секунду, пока не получит 2xx,
 *     и остановка на «уже обработано» сломала бы S8 (pending никогда
 *     не стал бы 0). Начисление при этом — ровно один раз (см.
 *     BonusRepository).
 *     Невалидное событие → 400 (обработан GlobalExceptionHandler'ом).
 *
 * GET /users/{id}/bonus — текущий баланс; у покупателя без начислений 0.
 *
 * Тонкий адаптер, как и OrdersController: валидация и правило
 * «ровно один раз» — в BonusService/BonusRepository.
 */
@RestController
@RequiredArgsConstructor
public class DeliveryController {

    private final BonusService bonusService;

    @PostMapping("/webhooks/delivery")
    public ResponseEntity<Void> deliveryConfirmed(@RequestBody DeliveryConfirmed event) {
        bonusService.confirmDelivery(event);
        // 204 и для первой доставки, и для копии: ответ одинаков, разница
        // только в том, начислили ли мы (это внутреннее дело, шине
        // важно лишь «2xx — не повторяй»).
        return ResponseEntity.noContent().build();
    }

    @GetMapping("/users/{id}/bonus")
    public BonusBalance bonus(@PathVariable("id") String id) {
        return bonusService.balance(id);
    }
}
