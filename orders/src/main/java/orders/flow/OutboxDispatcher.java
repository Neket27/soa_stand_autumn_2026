package orders.flow;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import orders.store.OutboxRepository;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Future;
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

    /** Размер пачки claim'а: 100 событий — один round-trip на отправку и пометку. */
    private static final int BATCH_SIZE = 100;

    /**
     * Тайм-аут видимости claim'а: если реплика упала между claim и
     * markSent, строку заберёт другая реплика через 60 с. Больше границы
     * ожидания доставки (get(15 с) выше): живая реплика успевает либо
     * пометить пачку, либо отпустить тик.
     */
    private static final Duration VISIBILITY_TIMEOUT = Duration.ofSeconds(60);

    private final OutboxRepository outboxRepository;
    private final KafkaTemplate<String, String> kafkaTemplate;

    /**
     * Тик поллинга: каждые 200 мс берём пачку неотправленных событий.
     * fixedDelay (не fixedRate) — следующий тик начнётся только после
     * завершения предыдущего, пачки не накладываются.
     */
    @Scheduled(fixedDelay = 200)
    public void pump() {
        // Claim выдаёт пачку ТОЛЬКО этой реплике (SKIP LOCKED + claimed_at):
        // остальные 4 реплики в этом же тике возьмут другие строки или
        // ничего. Раньше все читали одни и те же 100 строк — Kafka получала
        // события ~3,3-кратно, а UPDATE sent_at по одним строкам из 5 реплик
        // давал блокировки в Postgres (см. javadoc OutboxRepository.claimBatch).
        List<OutboxRepository.OutboxEvent> events =
                outboxRepository.claimBatch(BATCH_SIZE, VISIBILITY_TIMEOUT);
        if (events.isEmpty()) {
            return;
        }
        try {
            // Отправляем пачку ПАРАЛЛЕЛЬНО: send() асинхронный, сеть и
            // батчинг продюсера работают на всех событиях одновременно.
            // Раньше было последовательное get() на каждое событие — тик
            // стоил N round-trips и не поспевал за потоком заказов на
            // высоких темпах.
            //
            // get(15, SECONDS) — ждём подтверждения Kafka по ВСЕМ событиям
            // пачки и только после этого помечаем: «в никуда» не отправляем
            // (fire-and-forget подтвердил бы markSent доставки, которой
            // не было).
            //
            // Таймаут как страховка поверх конфига продюсера:
            // delivery.timeout.ms = 10 с ограничивает только future, а
            // send() до создания future блокируется максимум на
            // max.block.ms = 5 с (KafkaConfig). 15 с > 10 с + 5 с.
            List<Future<SendResult<String, String>>> futures = new ArrayList<>(events.size());
            for (OutboxRepository.OutboxEvent event : events) {
                futures.add(kafkaTemplate.send("orders-charge", event.orderId(), event.payload()));
            }
            for (Future<SendResult<String, String>> future : futures) {
                future.get(15, TimeUnit.SECONDS);
            }
            outboxRepository.markSent(events.stream()
                    .map(OutboxRepository.OutboxEvent::id)
                    .toList());
        } catch (InterruptedException e) {
            // Нас прервали (остановка приложения): восстанавливаем флаг,
            // иначе шедулер не сумеет корректно завершить поток. События
            // уже заявлены: они вернутся к другим репликам (или к нам после
            // рестарта) по истечении visibility timeout — дубль безопасен.
            Thread.currentThread().interrupt();
            log.info("Outbox: отправка прервана, выходим из тика");
        } catch (Exception e) {
            // Kafka недоступна: пачка остаётся непомеченной, повтор — когда
            // claim-тайм-аут истечёт (или Kafka поднимется раньше и чей-то
            // тик заберёт пачку заново). Прерывать тик не нужно — остальные
            // события пачки уже в очереди продюсера.
            log.warn("Outbox: не удалось отправить пачку из {} событий, повтор после claim-тайм-аута: {}",
                    events.size(), e.getMessage());
        }
    }
}
