package orders.inventory;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;
import orders.api.dto.Item;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * HTTP-клиент для Inventory API.
 *
 * === КАК РАБОТАЕТ ===
 * 1. Резерв: POST {INVENTORY_URL}/reservations
 *    {
 *      "order_id": "...",
 *      "items": [{"sku": "...", "qty": 1}]
 *    }
 *
 * 2. Снятие резерва: DELETE {INVENTORY_URL}/reservations/{orderId}
 *
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 *
 * S1: При отклонении банком (402) нужно снять резерв.
 *     — Проверь, что releaseReservation() вызывается при DECLINED
 *     — Добавь retry для DELETE /reservations/{id} при ошибке
 *
 * S2: Если снятие резерва упадёт — заказ останется REJECTED
 *     без снятого резерва → S1 упадёт.
 *     — Нужен механизм повторного снятия (reaper или retry)
 */
@Component
public class InventoryClient {

    private final RestTemplate restTemplate = new RestTemplate();

    @Value("${INVENTORY_URL}")
    private String inventoryUrl;

    /**
     * Резервирует товары для заказа.
     * Вызывается ДО создания заказа в PENDING.
     *
     * Если Inventory вернёт ошибку — заказ не создаётся.
     * Это правильное поведение: нет резерва → нет заказа.
     */
    public void reserve(String orderId, List<Item> items) {
        List<Map<String, Object>> itemsPayload = items.stream()
                .map(i -> {
                    Map<String, Object> m = new HashMap<>();
                    m.put("sku", i.sku());
                    m.put("qty", i.qty());
                    return m;
                })
                .toList();

        Map<String, Object> body = Map.of(
                "order_id", orderId,
                "items", itemsPayload
        );

        restTemplate.postForEntity(inventoryUrl + "/reservations", body, Void.class);
    }

    /**
     * Снимает резерв товара.
     * Вызывается при отклонении банком (402).
     *
     * === ЧТО СДЕЛАТЬ САМОМУ (S1) ===
     *
     * Если DELETE упадёт (500 от Inventory или сетевая ошибка),
     * резерв останется, и S1 упадёт с ошибкой:
     * "rejected без снятого резерва"
     *
     * Решение:
     * 1. Добавить retry (3 попытки с backoff)
     * 2. Или записать в таблицу "release_duty" и повторить позже
     */
    public void releaseReservation(String orderId) {
        // TODO(S1): добавить retry при ошибке
        // Например:
        // for (int i = 0; i < 3; i++) {
        //     try {
        //         restTemplate.delete(inventoryUrl + "/reservations/" + orderId);
        //         return;
        //     } catch (Exception e) {
        //         Thread.sleep(1000 * (i + 1));
        //     }
        // }
        // throw new RuntimeException("Не удалось снять резерв для " + orderId);

        restTemplate.delete(inventoryUrl + "/reservations/" + orderId);
    }
}
