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
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Делегирование BonusService.
 *
 * Идемпотентность «ровно один раз» живёт в BonusRepository (транзакция
 * журнал+баланс) и проверяется сценарием S8 на стенде. Валидация входного
 * DTO проверяется отдельно через Bean Validation.
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
    void отсутствие_самого_события_отклоняется() {
        assertThrows(IllegalArgumentException.class, () -> bonusService.confirmDelivery(null));
    }

    @Test
    void баланс_незнакомого_покупателя_нулевой() {
        when(bonusRepository.points("u-0")).thenReturn(0L);
        assertEquals(0, bonusService.balance("u-0").points());

        when(bonusRepository.points("u-3")).thenReturn(120L);
        assertEquals(120, bonusService.balance("u-3").points());
    }
}
