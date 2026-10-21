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
     * Обработать проверенное событие доставки. Входной HTTP DTO
     * валидируется до вызова сервиса в DeliveryController.
     *
     * @return true — начислено впервые; false — это была копия
     */
    public boolean confirmDelivery(DeliveryConfirmed event) {
        if (event == null)
            throw new IllegalArgumentException("событие обязательно");

        return bonusRepository.applyDeliveryEventOnce(event);
    }

    /** Баланс покупателя; у покупателя без начислений — 0. */
    public BonusBalance balance(String userId) {
        return new BonusBalance(bonusRepository.points(userId));
    }
}
