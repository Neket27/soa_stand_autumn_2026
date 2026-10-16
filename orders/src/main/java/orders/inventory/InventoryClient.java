package orders.inventory;

import com.fasterxml.jackson.databind.ObjectMapper;
import orders.api.dto.InventoryReserveItemsDto;
import orders.api.dto.ItemPayloadInventory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.http.converter.HttpMessageConverter;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;
import orders.api.dto.Item;

import java.time.Duration;
import java.util.List;

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
 * === НАДЁЖНОСТЬ СНЯТИЯ РЕЗЕРВА ===
 *
 * Если DELETE упадёт (500 от Inventory или сетевая ошибка), исключение
 * уходит в Consumer: он ставит release_pending = TRUE и повторяет снятие
 * на каждом тике Reaper, пока не получит успех. Заказ не «застревает»
 * с действующим резервом.
 */
@Component
public class InventoryClient {

    private final RestTemplate restTemplate;

    @Value("${INVENTORY_URL}")
    private String inventoryUrl;

    // Таймауты обязательны: reserve() вызывается В HTTP-ПУТИ POST /orders,
    // и зависший Inventory без них подвесил бы весь запрос (бюджет 1 с на
    // POST, S3/S4). При сбое получаем исключение → заказ не создаётся.
    public InventoryClient(ObjectMapper objectMapper) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(5));
        factory.setReadTimeout(Duration.ofSeconds(10));
        RestTemplate restTemplate = new RestTemplate(factory);
        // Подставляем контекстный ObjectMapper: только он знает snake_case-
        // стратегию (JacksonConfig). Без неё DTO ушёл бы в JSON как
        // {"orderId":...}, а Inventory ждёт {"order_id":...}.
        for (HttpMessageConverter<?> converter : restTemplate.getMessageConverters()) {
            if (converter instanceof MappingJackson2HttpMessageConverter jackson) {
                jackson.setObjectMapper(objectMapper);
            }
        }
        this.restTemplate = restTemplate;
    }

    /**
     * Резервирует товары для заказа.
     * Вызывается ДО создания заказа в PENDING.
     *
     * Если Inventory вернёт ошибку — заказ не создаётся.
     * Это правильное поведение: нет резерва → нет заказа.
     */
    public void reserve(String orderId, List<Item> items) {
        List<ItemPayloadInventory> itemsPayload = items.stream()
                .map(i -> new ItemPayloadInventory(i.sku(), i.qty()))
                .toList();

        InventoryReserveItemsDto itemsDto = new InventoryReserveItemsDto(orderId, itemsPayload);

        restTemplate.postForEntity(inventoryUrl + "/reservations", itemsDto, Void.class);
    }

    /**
     * Снимает резерв товара.
     * Вызывается при отклонении банком (402).
     *
     * При ошибке исключение НЕ глотаем: его получает Consumer, который
     * помечает заказ release_pending = TRUE, и снятие повторяет Reaper
     * (см. javadoc класса).
     */
    public void releaseReservation(String orderId) {
        restTemplate.delete(inventoryUrl + "/reservations/" + orderId);
    }
}
