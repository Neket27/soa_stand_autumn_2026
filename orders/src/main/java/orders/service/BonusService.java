package orders.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import orders.api.dto.BonusBalance;
import orders.api.dto.DeliveryConfirmed;
import orders.store.BonusRepository;

/**
 * Сервис начисления бонусов за подтверждённую доставку (задание 2).
 *
 * === БИЗНЕС-ПРАВИЛО ===
 *
 * За каждое событие delivery.confirmed покупателю user_id начисляются
 * bonus_points РОВНО ОДИН РАЗ, независимо от того, сколько копий
 * события прислала шина. Идемпотентность — в BonusRepository (журнал
 * bonus_events + одна транзакция на «журнал + баланс»).
 *
 * === ПОЧЕМУ БЕЗ ПОИСКА ЗАКАЗА ===
 *
 * Событие приходит от службы доставки и не ссылается на заказ
 * (contracts/orders.yaml: «начисляйте бонусы по user_id из события,
 * не исяка заказ»). Поэтому здесь нет ни OrderRepository, ни каких-либо
 * проверок существования заказа — только журнал и баланс.
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class BonusService {

    private final BonusRepository bonusRepository;

    /**
     * Обработать событие доставки. Бросает IllegalArgumentException при
     * невалидном событии — GlobalExceptionHandler превратит это в 400.
     *
     * @return true — начислено впервые; false — это была копия
     */
    public boolean confirmDelivery(DeliveryConfirmed event) {
        validate(event);
        return bonusRepository.applyDeliveryEventOnce(event);
    }

    /** Баланс покупателя; у покупателя без начислений — 0. */
    public BonusBalance balance(String userId) {
        return new BonusBalance(bonusRepository.points(userId));
    }

    /**
     * Валидация по контракту Events. Начисление по «мусорному» событию —
     * это потеря/искажение бонусов, поэтому проверяем ДО транзакции.
     */
    private void validate(DeliveryConfirmed event) {
        if (event == null || isBlank(event.id()) || isBlank(event.userId())) {
            throw new IllegalArgumentException("событие без id или user_id");
        }
        if (!DeliveryConfirmed.TYPE.equals(event.type())) {
            throw new IllegalArgumentException("неизвестный тип события: " + event.type());
        }
        if (event.bonusPoints() < 1) {
            throw new IllegalArgumentException("bonus_points должен быть не меньше 1");
        }
    }

    private boolean isBlank(String s) {
        return s == null || s.isBlank();
    }
}
