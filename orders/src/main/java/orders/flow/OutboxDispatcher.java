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

import java.sql.Timestamp;
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
 * Если Kafka недоступна — send() падает, строки остаются без sent_at и
 * claimed до истечения visibility timeout (60 с). Позднее поллер снова
 * заберёт их из БД; ближайший тик может обработать другие строки.
 *
 * === СВОЙСТВА ДОСТАВКИ ===
 *
 * At-least-once: если упасть МЕЖДУ send() и markSent(), событие будет
 * отправлено повторно. Дубликаты безопасны:
 *   — Consumer проверяет статус заказа и пропускает уже обработанные;
 *   — Idempotency-Key в Payment защищает от двойного списания.
 */

@Slf4j
@Component
@RequiredArgsConstructor
public class OutboxDispatcher {

    /** Размер пачки claim'а: 100 событий — один round-trip на отправку и пометку. */
    private static final int BATCH_SIZE = 100;

    /**
     * Тайм-аут видимости claim'а: если реплика упала между claim и
     * markSent, строку заберёт другая реплика через 60 с. Это lease для
     * восстановления после остановки/сбоя реплики; отправка пачки может
     * включать несколько вызовов send(), поэтому 60 с не является общим
     * дедлайном одного тика.
     */
    private static final Duration VISIBILITY_TIMEOUT = Duration.ofSeconds(60);

    private final OutboxRepository outboxRepository;
    private final KafkaTemplate<String, String> kafkaTemplate;

    /**
     * Тик поллинга: каждые 200 мс берём пачку неотправленных событий.
     * fixedDelay (не fixedRate) — у этой реплики следующий тик начнётся
     * только после завершения предыдущего; на других репликах поллеры
     * работают независимо, а claim разделяет строки между ними.
     */
    @Scheduled(fixedDelay = 200)
    public void pump() {
        // Claim выдаёт пачку этому конкурентному запросу; остальные
        // конкурентные запросы пропускают эти строки (SKIP LOCKED) и могут
        // забрать другие. Claim — lease, а не блокировка на всё время
        // отправки: после его истечения строку может забрать другая реплика,
        // даже если прежняя ещё обрабатывает пачку.
        // Раньше все читали одни и те же строки, что давало дубли Kafka и
        // блокировки UPDATE (см. javadoc OutboxRepository.claimBatch).
        List<OutboxRepository.OutboxEvent> events =
                outboxRepository.claimBatch("order.charge", BATCH_SIZE, VISIBILITY_TIMEOUT);
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
            // Kafka недоступна: пачка остаётся непомеченной и claimed до
            // истечения visibility timeout. Отдельные send() могли успеть
            // передать часть запросов producer'у; повтор безопасен благодаря
            // идемпотентной обработке.
            log.warn("Outbox: не удалось отправить пачку из {} событий, повтор после claim-тайм-аута: {}",
                    events.size(), e.getMessage());
        }
    }

    /**
     * Чистка отправленных строк outbox (раз в час).
     *
     * === ЗАЧЕМ И ПОЧЕМУ ТАК (вопрос на защите «что растёт в базе») ===
     *
     * outbox_events растёт с каждым заказом (и charge, и paid события).
     * Отправленная строка (sent_at NOT NULL) свою роль сыграла: markSent —
     * подтверждение доставки, дубль возможен только в узком окне между
     * send и markSent. Стало быть, sent-строки старше часа удаляются
     * безо всякого риска для at-least-once.
     *
     * Свежие sent-строки НЕ трогаем: окно «send → markSent → реплика
     * упала → повтор из-за visibility timeout» ещё может сыграть, и
     * строка обязана быть на месте.
     *
     * Незасланные строки не трогаем никогда — они и есть очередь.
     */
    @Scheduled(fixedDelay = 3_600_000, initialDelay = 3_600_000)
    public void cleanupSent() {
        try {
            int removed = outboxRepository.deleteSentBefore(
                    Timestamp.from(java.time.Instant.now().minus(java.time.Duration.ofHours(1))));
            if (removed > 0) {
                log.info("Outbox: удалено {} отправленных событий старше часа", removed);
            }
        } catch (Exception e) {
            log.warn("Outbox: чистка не удалась (не критично): {}", e.getMessage());
        }
    }
}
