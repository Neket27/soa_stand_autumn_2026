package orders.api.dto;

import jakarta.validation.Validation;
import jakarta.validation.Validator;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class DeliveryConfirmedValidationTest {

    private final Validator validator = Validation.buildDefaultValidatorFactory().getValidator();

    @Test
    void корректное_событие_проходит_валидацию() {
        DeliveryConfirmed event = new DeliveryConfirmed(
                "evt-1", DeliveryConfirmed.TYPE, "u-3", 40, "2026-10-14T12:00:05Z");

        assertTrue(validator.validate(event).isEmpty());
    }

    @Test
    void обязательные_поля_и_тип_проверяются() {
        DeliveryConfirmed event = new DeliveryConfirmed(
                " ", "order.paid", null, 40, "2026-10-14T12:00:05Z");

        var violations = validator.validate(event);

        assertTrue(violations.stream().anyMatch(v -> v.getPropertyPath().toString().equals("id")));
        assertTrue(violations.stream().anyMatch(v -> v.getPropertyPath().toString().equals("type")));
        assertTrue(violations.stream().anyMatch(v -> v.getPropertyPath().toString().equals("userId")));
    }

    @Test
    void бонусы_должны_быть_положительными() {
        DeliveryConfirmed zero = new DeliveryConfirmed(
                "evt-1", DeliveryConfirmed.TYPE, "u-3", 0, "2026-10-14T12:00:05Z");
        DeliveryConfirmed negative = new DeliveryConfirmed(
                "evt-2", DeliveryConfirmed.TYPE, "u-3", -5, "2026-10-14T12:00:05Z");

        assertFalse(validator.validate(zero).isEmpty());
        assertFalse(validator.validate(negative).isEmpty());
    }
}
