package orders.api;

import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import orders.store.OrderRepository;

import java.util.Map;

/**
 * Health-check эндпоинт.
 *
 * Runner проверяет GET /healthz,.
 **/
@RestController
@RequiredArgsConstructor
public class HealthController {

    private final OrderRepository orderRepository;

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
