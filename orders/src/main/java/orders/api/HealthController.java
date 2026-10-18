package orders.api;

import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import orders.store.OrderRepository;
import orders.warmup.WarmupRunner;

import java.util.Map;

/**
 * Health-check эндпоинт.
 *
 * Runner проверяет GET /healthz.
 *
 * === ГЕЙТ ПРОГРЕВА ===
 *
 * Пока WarmupRunner не закончил прогрев (см. его javadoc), отвечаем 503:
 * раннер продолжает ждать и НЕ начинает слать 1000 заказов/с на холодную
 * JVM. Так хвост латентности первых секунд (стресс-тест: 865/12000
 * ответов дольше 1000 мс) уходит в окно ожидания, а не в бюджет прогона.
 * Ошибка БД (isReady) обрабатывается как раньше — 503.
 **/
@RestController
@RequiredArgsConstructor
public class HealthController {

    private final OrderRepository orderRepository;

    @GetMapping("/healthz")
    public ResponseEntity<Map<String, String>> health() {
        if (!WarmupRunner.isReady()) {
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(Map.of("status", "warming_up"));
        }
        try {
            orderRepository.isReady();
            return ResponseEntity.ok(Map.of("status", "ok"));
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                    .body(Map.of("status", "unavailable"));
        }
    }
}
