package orders.api.dto;

import org.junit.jupiter.api.Test;

import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Свойства события order.paid, на которых держится S6/S7:
 * поля контракта, стабильный id у копий, читаемый JSON.
 */
class OrderPaidEventTest {

    @Test
    void фабрика_заполняет_поля_контракта() {
        OrderPaidEvent event = OrderPaidEvent.forOrder("o-1");

        assertEquals(OrderPaidEvent.TYPE, event.type());
        assertEquals("order.paid", event.type());
        assertEquals("o-1", event.orderId());
        // UUID-строка из контракта (example — uuid, limit 128 символов).
        assertEquals(36, event.id().length());
        assertDoesNotThrow(() -> UUID.fromString(event.id()));
    }

    @Test
    void payload_содержит_поля_из_contract_events_yaml() {
        OrderPaidEvent event = OrderPaidEvent.forOrder("o-42");
        String payload = event.payload();

        assertTrue(payload.contains("\"id\":\"" + event.id() + "\""));
        assertTrue(payload.contains("\"type\":\"order.paid\""));
        assertTrue(payload.contains("\"order_id\":\"o-42\""));
        assertTrue(payload.contains("\"occurred_at\":\""));
        // Не camelCase: шина ждёт snake_case (JacksonConfig и контракт).
        assertTrue(!payload.contains("orderId"), "payload должен быть snake_case");
    }

    @Test
    void повтор_того_же_события_даёт_тот_же_payload() {
        // Копия при ретрае шлёт payload из БД, а не пересоздаёт событие:
        // именно поэтому у всех копий один id. Здесь проверяем сам принцип —
        // один объект → одинаковое тело при любом числе обращений.
        OrderPaidEvent event = OrderPaidEvent.forOrder("o-7");
        assertEquals(event.payload(), event.payload());
    }

    @Test
    void два_заказа_получают_разные_id() {
        assertNotEquals(
                OrderPaidEvent.forOrder("o-1").id(),
                OrderPaidEvent.forOrder("o-1").id());
    }
}
