package orders.service;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import orders.api.dto.DeliveryConfirmed;
import orders.store.BonusRepository;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Правила валидации и делегирования BonusService.
 *
 * Идемпотентность «ровно один раз» живёт в BonusRepository (транзакция
 * журнал+баланс) и проверяется сценарием S8 на стенде; здесь — то, что
 * стоит ДО транзакции.
 */
@ExtendWith(MockitoExtension.class)
class BonusServiceTest {

    @Mock
    private BonusRepository bonusRepository;

    @InjectMocks
    private BonusService bonusService;

    private DeliveryConfirmed event(String id, String type, String userId, long points) {
        return new DeliveryConfirmed(id, type, userId, points, "2026-10-14T12:00:05Z");
    }

    @Test
    void валидное_событие_уходит_в_репозиторий() {
        when(bonusRepository.applyDeliveryEventOnce(any())).thenReturn(true);

        assertTrue(bonusService.confirmDelivery(
                event("evt-1", "delivery.confirmed", "u-3", 40)));

        ArgumentCaptor<DeliveryConfirmed> captor = ArgumentCaptor.forClass(DeliveryConfirmed.class);
        verify(bonusRepository).applyDeliveryEventOnce(captor.capture());
        assertEquals("u-3", captor.getValue().userId());
        assertEquals(40, captor.getValue().bonusPoints());
    }

    @Test
    void копия_уже_обработанного_события_не_бросает_исключение() {
        when(bonusRepository.applyDeliveryEventOnce(any())).thenReturn(false);

        // 2xx и для копии — иначе шина доставляет вечно (S8).
        assertFalse(bonusService.confirmDelivery(
                event("evt-1", "delivery.confirmed", "u-3", 40)));
    }

    @Test
    void пустые_id_или_user_id_отклоняются_до_транзакции() {
        assertThrows(IllegalArgumentException.class, () -> bonusService.confirmDelivery(
                event("", "delivery.confirmed", "u-3", 40)));
        assertThrows(IllegalArgumentException.class, () -> bonusService.confirmDelivery(
                event("evt-1", "delivery.confirmed", " ", 40)));
        assertThrows(IllegalArgumentException.class, () -> bonusService.confirmDelivery(
                event(null, "delivery.confirmed", "u-3", 40)));
        verify(bonusRepository, never()).applyDeliveryEventOnce(any());
    }

    @Test
    void чужой_тип_события_отклоняется() {
        assertThrows(IllegalArgumentException.class, () -> bonusService.confirmDelivery(
                event("evt-1", "order.paid", "u-3", 40)));
        verify(bonusRepository, never()).applyDeliveryEventOnce(any());
    }

    @Test
    void нулевые_и_отрицательные_бонусы_отклоняются() {
        // Контракт: bonus_points minimum: 1. Начислять 0 (или убыток) —
        // искажение бизнес-правила, поэтому отказ до транзакции.
        assertThrows(IllegalArgumentException.class, () -> bonusService.confirmDelivery(
                event("evt-1", "delivery.confirmed", "u-3", 0)));
        assertThrows(IllegalArgumentException.class, () -> bonusService.confirmDelivery(
                event("evt-1", "delivery.confirmed", "u-3", -5)));
        verify(bonusRepository, never()).applyDeliveryEventOnce(any());
    }

    @Test
    void баланс_незнакомого_покупателя_нулевой() {
        when(bonusRepository.points("u-0")).thenReturn(0L);
        assertEquals(0, bonusService.balance("u-0").points());

        when(bonusRepository.points("u-3")).thenReturn(120L);
        assertEquals(120, bonusService.balance("u-3").points());
    }
}
