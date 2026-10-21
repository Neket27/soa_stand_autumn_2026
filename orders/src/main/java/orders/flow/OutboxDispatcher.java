package orders.flow;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
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
import java.util.concurrent.atomic.AtomicInteger;

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
 *
 * === BACKOFF ПРИ ОТКАЗЕ KAFKA ===
 *
 * Если Kafka недоступна, упавшая пачка остаётся claimed до истечения
 * visibility timeout (60 с) — сама она не переотправляется на каждом
 * тике. Но поллер продолжал бы каждые 200 мс забирать новые пачки и
 * штамповать WARN: при затяжном отказе это лишняя нагрузка на брокер
 * и шум в логах.
 *
 * Поэтому при ошибке отправки поллер засыпает на экспоненциальный
 * интервал 200 мс × 2^n (первый отказ — 200 мс, второй — 400 мс, ...,
 * потолок 60 с), после успеха сбрасывается в ноль. Состояние локальное
 * для реплики: общий счётчик не нужен, каждая реплика затихает и
 * просыпается независимо. После подъёма Kafka очередь уходит не позже
 * чем через MAX_BACKOFF — раньше это же время давал claim-lease, так
 * что время восстановления не выросло.
 *
 * === РУЧНАЯ ПРОВЕРКА (S2) ===
 *
 * Убедись, что при недоступной Kafka события копятся в outbox
 * (sent_at IS NULL), а после её подъёма уходят (логи, /admin/facts).
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

    /** Первый экспоненциальный интервал после неудачной отправки (см. backoffDelayMillis). */
    private static final long MIN_BACKOFF_MILLIS = 200;

    /** Потолок экспоненциального backoff'а: после подъёма Kafka очередь уйдёт не позже чем через 60 с. */
    private static final long MAX_BACKOFF_MILLIS = 60_000;

    private final OutboxRepository outboxRepository;
    private final KafkaTemplate<String, String> kafkaTemplate;

    /**
     * Неудачные отправки подряд (для экспоненциального backoff'а).
     * Сейчас тиком занимается один поток шедулера (fixedDelay), так что
     * хватило бы и обычного int; atomic — чтобы инкремент оставался
     * корректным, если тик когда-нибудь вынесут в отдельный поток.
     */
    private final AtomicInteger consecutiveFailures = new AtomicInteger();

    /**
     * nanoTime следующей разрешённой попытки: пока оно не наступило,
     * pump() выходит сразу, не ходя даже в БД за claim'ом. 0 — «backoff
     * не действует». volatile — значение пишется в конце тика (успех или
     * ошибка) и читается в начале следующего; volatile не даёт потоку
     * шедулера работать с закэшированным значением.
     */
    private volatile long nextAttemptNanos;

    /**
     * Тик поллинга: каждые 200 мс берём пачку неотправленных событий.
     * fixedDelay (не fixedRate) — у этой реплики следующий тик начнётся
     * только после завершения предыдущего; на других репликах поллеры
     * работают независимо, а claim разделяет строки между ними.
     */
    @Scheduled(fixedDelay = 200)
    public void pump() {
        // Backoff: после неудачной отправки не ходим в БД за claim'ом до
        // наступления nextAttemptNanos. Сравнение через разность
        // (deadline - сейчас > 0)
        if (nextAttemptNanos != 0 && nextAttemptNanos - System.nanoTime() > 0) {
            return;
        }
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
            // Отправка прошла — backoff сбрасывается: следующий тик через
            // обычные 200 мс, даже если перед этим были неудачи.
            consecutiveFailures.set(0);
            nextAttemptNanos = 0;
        } catch (InterruptedException e) {
            // Нас прервали (остановка приложения): восстанавливаем флаг,
            // иначе шедулер не сумеет корректно завершить поток. События
            // уже заявлены: они вернутся к другим репликам (или к нам после
            // рестарта) по истечении visibility timeout — дубль безопасен.
            // Backoff не трогаем: тик прерван, а не упал из-за Kafka —
            // после рестарта поллер начнёт работу с чистого листа.
            Thread.currentThread().interrupt();
            log.info("Outbox: отправка прервана, выходим из тика");
        } catch (Exception e) {
            // Kafka недоступна: пачка остаётся непомеченной и claimed до
            // истечения visibility timeout. Отдельные send() могли успеть
            // передать часть запросов producer'у; повтор безопасен благодаря
            // идемпотентной обработке.
            //
            // Саму пачку claim и так не даст переотправить до истечения
            // lease, а вот НОВЫЕ пачки поллер без backoff'а забирал бы
            // каждые 200 мс. Засыпаем экспоненциально: 200 мс, 400 мс, ...,
            // до 60 с.
            long backoff = backoffDelayMillis(consecutiveFailures.incrementAndGet());
            nextAttemptNanos = System.nanoTime() + backoff * 1_000_000;
            log.warn("Outbox: не удалось отправить пачку из {} событий ({}-я неудача подряд), "
                            + "следующая попытка через {} мс; упавшая пачка ждёт claim-тайм-аут: {}",
                    events.size(), consecutiveFailures.get(), backoff, e.getMessage());
        }
    }

    /**
     * Экспоненциальный интервал до следующей попытки: MIN_BACKOFF × 2^(n-1),
     * ограничен сверху MAX_BACKOFF.
     *
     * n — число неудач подряд, начиная с 1: первый отказ → 200 мс,
     * второй → 400 мс, третий → 800 мс, ... Всё выше потолка (60 с)
     * не растёт: дольше «молчать» смысла нет — столько же в худшем случае
     * ждёт и истечение claim-lease, плюс при подъёме Kafka очередь должна
     * уйти разумно быстро.
     *
     * Выделено в чистую функцию, чтобы рост задержки проверялся без
     * реального ожидания (см. OutboxDispatcherTest).
     */
    static long backoffDelayMillis(int consecutiveFailures) {
        long shift = Math.min(consecutiveFailures - 1, 16);
        return Math.min(MIN_BACKOFF_MILLIS << shift, MAX_BACKOFF_MILLIS);
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
