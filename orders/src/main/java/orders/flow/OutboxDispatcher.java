package orders.flow;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import orders.store.OutboxRepository;

import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * Фоновый процесс Transactional Outbox: читает outbox_events,
 * шлёт неотправленные события в Kafka и помечает их sent_at.
 *
 * === ЗАЧЕМ ===
 *
 * CreateOrderService пишет событие в outbox ВМЕСТЕ с заказом (одна
 * транзакция). Этот класс — «доставщик»: периодически выбирает строки
 * с sent_at IS NULL, публикует их в топик и помечает отправленными.
 *
 * Если Kafka недоступна — send() падает, строки остаются без sent_at,
 * следующий тик попробует снова. Событие не теряется: оно живёт в БД.
 *
 * === СВОЙСТВА ДОСТАВКИ ===
 *
 * At-least-once: если упасть МЕЖДУ send() и markSent(), событие будет
 * отправлено повторно. Дубликаты безопасны:
 *   — Consumer проверяет статус заказа и пропускает уже обработанные;
 *   — Idempotency-Key в Payment защищает от двойного списания.
 *
 * === ЧТО НЕ СДЕЛАНО / ЧТО ПРОВЕРИТЬ САМОМУ ===
 *
 * S2 (ручная проверка): убедись, что при недоступной Kafka события
 *     копятся в outbox, а после её подъёма уходят (логи, /admin/facts).
 *
 * S4 (не реализовано): backoff для поллинга. Сейчас при упавшей Kafka
 *     каждый тик упирается в max.block.ms (5 с), и send() повторяется
 *     каждые ~5 с всё время отказа. События при этом не теряются (они в
 *     БД), но Kafka долбится без нарастающей паузы — имеет смысл
 *     экспоненциальный backoff до разумного максимума.
 */

@Slf4j
@Component
@RequiredArgsConstructor
public class OutboxDispatcher {

    private final OutboxRepository outboxRepository;
    private final KafkaTemplate<String, String> kafkaTemplate;

    /**
     * Тик поллинга: каждые 200 мс берём пачку неотправленных событий.
     * fixedDelay (не fixedRate) — следующий тик начнётся только после
     * завершения предыдущего, пачки не накладываются.
     */
    @Scheduled(fixedDelay = 200)
    public void pump() {
        List<OutboxRepository.OutboxEvent> events = outboxRepository.findUnsent(100);
        for (OutboxRepository.OutboxEvent event : events) {
            try {
                // get(15, SECONDS) — ждём подтверждения от Kafka: только после
                // успешной отправки помечаем строку. Если бы отправляли
                // «в никуда» (fire-and forget), markSent подтвердил бы
                // доставку, которой не было.
                //
                // Таймаут нужен как страховка поверх конфига продюсера:
                // delivery.timeout.ms = 10 с ограничивает только future, а
                // send() до создания future блокируется максимум на
                // max.block.ms = 5 с (KafkaConfig). 15 с > 10 с + 5 с, то
                // есть в норме сработает раньше продюсерский таймаут, но
                // обезопасит от зависшей Sender-нити или будущей правки
                // конфига: get() без таймаута не имеет собственной границы.
                kafkaTemplate.send("orders-charge", event.orderId(), event.payload())
                        .get(15, TimeUnit.SECONDS);
                outboxRepository.markSent(event.id());
            } catch (InterruptedException e) {
                // Нас прервали (остановка приложения): восстанавливаем флаг,
                // иначе шедулер не сумеет корректно завершить поток, и
                // выходим из пачки — события уйдут на следующем тике.
                Thread.currentThread().interrupt();
                log.info("Outbox: отправка прервана, выходим из тика");
                return;
            } catch (Exception e) {
                // Kafka недоступна: строки остаются без sent_at,
                // попробуем на следующем тике. Прерываем пачку —
                // если Kafka лежит, нет смысла падать на каждой строке.
                log.warn("Outbox: не удалось отправить событие {} (заказ {}), повтор на следующем тике: {}",
                        event.id(), event.orderId(), e.getMessage());
                return;
            }
        }
    }
}
