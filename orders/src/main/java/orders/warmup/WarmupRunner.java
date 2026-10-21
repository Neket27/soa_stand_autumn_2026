package orders.warmup;

import lombok.extern.slf4j.Slf4j;
import orders.api.dto.Item;
import orders.api.dto.OrderCreated;
import orders.api.dto.OrderRequest;
import orders.store.OrderRepository;
import orders.store.OutboxRepository;
import orders.store.WarmupBarrierRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Прогрев сервиса ДО того, как /healthz начнёт отвечать 200.
 *
 * === ЗАЧЕМ (по историческому стресс-тесту на 1000 заказов/с) ===
 *
 * Хвост латентности (865/12000 ответов дольше 1000 мс, макс 2.97 с)
 * целиком приходился на ПЕРВЫЕ ~3 СЕКУНДЫ прогона: дальше SLOW = 0.
 * Причина — холодный старт ВСЕЙ цепи одновременно:
 *   - JVM прогревается на живом трафике (JIT, class loading,
 *     Jackson/валидация/HTTP-клиент);
 *   - Apache HttpClient-пул стартует пустым: каждому первому reserve()
 *     нужно новое соединение к mocks;
 *   - свежий Postgres (пересоздаётся при каждом make up) прогревает
 *     буферы и JDBC-путь;
 *   - consumer'ы входят в consumer group Kafka и догоняют outbox —
 *     конкуренция за CPU/БД как раз в первые секунды.
 * В том нагрузочном прогоне раннер начинал отправку сразу после
 * GET /healthz = 200; текущие сценарии задают темп в runner/params.
 *
 * === КАК УСТРОЕН ПРОГРЕВ ===
 *
 * ApplicationRunner выполняется ПОСЛЕ старта Tomcat (порт уже слушается)
 * и ДО статуса ready. Пока прогрев не закончен, HealthController отвечает
 * 503. Прогрев выполняет внутренние POST-запросы и пытается дренировать
 * созданные заказы; ожидания ограничены таймаутами и работают fail-open:
 *   1. N POST /orders в себя же (через общий RestTemplate): прогревает
 *      Tomcat-потоки, валидацию, Jackson, контроллер, reserve() через
 *      пул соединений, транзакцию с двумя INSERT'ами, outbox, и оплату
 *      коньюмерами через Kafka (точный боевой путь от и до);
 *   2. ждёт выхода прогрев-заказов из PENDING в пределах заданного
 *      timeout; при его истечении продолжает запуск с предупреждением.
 *
 * === ПРОГРЕВ И ФАКТЫ СЦЕНАРИЕВ ===
 *
 * Раннер вызывает POST /admin/reset в заглушках в НАЧАЛЕ каждого
 * сценария — уже записанные факты прогрева (резервы, списания) стираются
 * до проверок. Однако дренаж заказов и событий ограничен таймаутами и
 * работает fail-open: если он не завершился, поздние факты прогрева могут
 * попасть в сценарий.
 */
@Slf4j
@Component
public class WarmupRunner implements ApplicationRunner {

    /**
     * Сколько заказов создаём на прогреве при ХОЛОДНОМ старте.
     * Значение применяется на каждой реплике, запускающей WarmupRunner.
     *
     * Объём 3000 и темп ниже были выбраны по отдельному стресс-тесту
     * (5000/с по стенду), а не по текущим темпам сценариев runner.
     * В том тесте хвост целиком приходился на первые ~1,5 с после старта
     * отправки (Tomcat раскручивает потоки с 5 до 168, коньюмеры получают
     * первую волну событий, Postgres входит в полный темп записи) — и
     * всё это на «непрогретом темпе». Настроенный темп прогрева —
     * 2000 заказов/с на реплику и также является отдельной нагрузочной
     * настройкой, а не текущим темпом штатных сценариев.
     */
    private static final int WARMUP_ORDERS_COLD = 3000;

    /**
     * Прогрев при РЕСТАРТЕ сервиса на живой БД (сценарии S7/S8 перезапускают
     * Orders посреди прогона): код тот же, а вот «входить в боевой темп»
     * заново не нужно — резкий прогрев посреди работающего сценария сам
     * стал бы источником ложной нагрузки и чужих фактов. 120 заказов —
     * прежний, проверенный сценариями объём (JVM-старт + связки).
     */
    private static final int WARMUP_ORDERS_RESTART = 120;

    /** Темп прогрева на реплику, заказов/с (нагрузочная настройка, не темп штатных сценариев). */
    private static final double WARMUP_RATE_PER_S = 2000;

    /**
     * Максимум запросов в полёте. Ограничивает очередь при разгоне:
     * без границы узкое место прогрева (а не боевой путь) породило бы
     * тысячи ожидающих потоков.
     */
    private static final int MAX_IN_FLIGHT = 64;

    /**
     * Сколько ждать выхода прогрев-заказов из PENDING. Fail-open:
     * при истечении прогрев всё равно завершается (с warning), иначе
     * /healthz завис бы навсегда и раннер упал бы по своему таймауту
     * без внятной причины.
     */
    private static final long DRAIN_TIMEOUT_MS = 20_000;

    /**
     * Сколько ждать, пока события оплаты прогрев-заказов (order.paid)
     * уйдут из outbox в шину. Отдельный бюджет от дренажа заказов:
     * заказы уже вне PENDING, осталось переслать события.
     *
     * === ЗАЧЕМ ЭТОТ ДРЕНАЖ ===
     *
     * События order.paid уходят в шину ФОНОМ (EventsPublisher, 200 мс).
     * Прогрев при холодном старте завершился бы по статусам заказов, а
     * публикация событий ещё шла бы — и тогда события прогрев-заказов
     * прилетали бы в шину УЖЕ во время S1: /admin/reset перед сценарием
     * стирает факты шины, но их списания к тому моменту стёрты тоже,
     * и проверка «paid: деньги списаны и товар зарезервирован» падала
     * бы на заказах «paid, но без списания в фактах» (найдено прогоном
     * задания 2). С дренажем события доходят до шины ДО /healthz=200,
     * а reset перед первым сценарием их затирает — факты чистые.
     *
     * Fail-open, как и остальные ожидания прогрева: не дождались —
     * идём дальше с warning, /healthz не должен зависнуть навсегда.
     */
    private static final long EVENT_DRAIN_TIMEOUT_MS = 15_000;

    /**
     * Сколько ждать, пока все реплики встанут в барьер готовности.
     * Fail-open: упавшая реплика не должна навечно держать кластер
     * в 503 (раннер ждёт healthz не дольше 120 с).
     */
    private static final long BARRIER_TIMEOUT_MS = 60_000;

    /** Пауза опроса барьера/дренажа. */
    private static final long POLL_INTERVAL_MS = 200;

    /** Готовность сервиса к внешнему трафику (см. HealthController). */
    private static volatile boolean ready = false;

    private final RestTemplate restTemplate;
    private final OrderRepository orderRepository;
    private final OutboxRepository outboxRepository;
    private final WarmupBarrierRepository warmupBarrierRepository;
    private final String selfOrdersUrl;
    private final int expectedPeers;

    public WarmupRunner(RestTemplate restTemplate,
                        OrderRepository orderRepository,
                        OutboxRepository outboxRepository,
                        WarmupBarrierRepository warmupBarrierRepository,
                        @Value("${PORT:8080}") int port,
                        // Дефолт 1: официальная проверка запускает одну реплику
                        // (compose из master курса, без deploy.replicas) — ждать
                        // больше нечего. Многорепликовый локальный запуск задаёт
                        // число реплик явно: WARMUP_BARRIER_EXPECTED=<N>.
                        @Value("${WARMUP_BARRIER_EXPECTED:1}") int expectedPeers) {
        this.restTemplate = restTemplate;
        this.orderRepository = orderRepository;
        this.outboxRepository = outboxRepository;
        this.warmupBarrierRepository = warmupBarrierRepository;
        this.selfOrdersUrl = "http://localhost:" + port + "/orders";
        this.expectedPeers = expectedPeers;
    }

    /** Пока false — /healthz отвечает 503, раннер ждёт. */
    public static boolean isReady() {
        return ready;
    }

    @Override
    public void run(ApplicationArguments args) {
        long t0 = System.nanoTime();
        // Пустой барьер = БД пересоздана = холодный старт кластера
        // (make up всегда force-recreate'ит postgres). Рестарт на живой
        // БД (S6/S7) — лёгкий прогрев, барьер уже собран прежним запуском.
        boolean coldBoot = warmupBarrierRepository.readyCount() == 0;
        int ordersToSend = coldBoot ? WARMUP_ORDERS_COLD : WARMUP_ORDERS_RESTART;
        List<String> ids = sendWarmupOrders(ordersToSend);
        long sentMs = (System.nanoTime() - t0) / 1_000_000;

        boolean drained = awaitDrain(ids);
        // Дренаж событий — только на холодном старте: события прогрева
        // должны по возможности уйти в шину ДО /healthz, чтобы reset перед S1 их затёр.
        // При рестарте (S7/S8) шина может быть намеренно недоступна —
        // события их прогрев-заказов обязаны уйти во время сценария
        // (проверки требуют событий для всех paid из фактов), поэтому
        // здесь дожидаться их было бы и вредно, и бессмысленно.
        boolean eventsDrained = !coldBoot || awaitEventDrain();
        joinBarrier();

        ready = true;
        long totalMs = (System.nanoTime() - t0) / 1_000_000;
        log.info("Warmup: {} заказов отправлено за {} мс ({}), дренаж {} ({}), события {} (итого {} мс)",
                ids.size(), sentMs, coldBoot ? "холодный старт" : "рестарт",
                drained ? "ok" : "TIMEOUT " + DRAIN_TIMEOUT_MS + " мс",
                coldBoot ? (eventsDrained ? "ok" : "TIMEOUT " + EVENT_DRAIN_TIMEOUT_MS + " мс") : "пропущен",
                totalMs);
    }

    /**
     * Барьер готовности: помечаем реплику и ждём, пока пометятся ВСЕ.
     *
     * Раннер стартует сценарий от /healthz=200 первой попавшейся реплики —
     * без барьера S1 начинался, пока остальные реплики ещё дренировали
     * прогрев, и их прогрев-заказы попадали в факты сценария (TASK=1:
     * 4624 чужих списания, проверка «paid без резерва» — FAIL). Когда
     * счётчик достигает порога, барьер пропускает запуск. Таймаут
     * барьера fail-open: /healthz=200 сам по себе не гарантирует, что все
     * активные реплики готовы или что дренаж завершился.
     */
    private void joinBarrier() {
        String replicaId = replicaId();
        warmupBarrierRepository.markReady(replicaId);
        long deadline = System.currentTimeMillis() + BARRIER_TIMEOUT_MS;
        while (true) {
            int collected = warmupBarrierRepository.readyCount();
            if (collected >= expectedPeers) {
                log.info("Warmup: барьер собран — {} реплик готовы ({})", collected, replicaId);
                return;
            }
            if (System.currentTimeMillis() > deadline) {
                log.warn("Warmup: барьер не собрался за {} мс ({} из {} реплик) — открываемся, "
                                + "готовность остальных реплик не гарантируется",
                        BARRIER_TIMEOUT_MS, collected, expectedPeers);
                return;
            }
            try {
                Thread.sleep(POLL_INTERVAL_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }

    /** Идентификатор реплики: hostname контейнера (стабилен при рестарте, уникален между репликами). */
    private static String replicaId() {
        try {
            return java.net.InetAddress.getLocalHost().getHostName();
        } catch (Exception e) {
            // Крайний случай: без hostname барьер всё равно должен работать,
            // но с «шумной» строкой — лучше падать в детерминированный вид.
            return "unknown-replica";
        }
    }

    /** Отправляет прогрев-заказы в себя же с заданным темпом, возвращает их id. */
    private List<String> sendWarmupOrders(int ordersToSend) {
        List<String> ids = new ArrayList<>();
        AtomicInteger failures = new AtomicInteger();
        AtomicInteger slow = new AtomicInteger();
        ExecutorService pool = Executors.newFixedThreadPool(MAX_IN_FLIGHT);
        Semaphore inFlight = new Semaphore(MAX_IN_FLIGHT);
        long start = System.nanoTime();
        try {
            for (int i = 0; i < ordersToSend; i++) {
                // Ровный темп WARMUP_RATE_PER_S:
                // не «пачка как получится», а разгон с нужной скоростью.
                long scheduledAt = start + (long) (i / WARMUP_RATE_PER_S * 1_000_000_000L);
                long sleepNanos = scheduledAt - System.nanoTime();
                if (sleepNanos > 0) {
                    TimeUnit.NANOSECONDS.sleep(sleepNanos);
                }
                inFlight.acquire();
                pool.submit(() -> {
                    try {
                        long t0 = System.nanoTime();
                        OrderRequest request = new OrderRequest(
                                "warmup-user",
                                List.of(new Item("WARMUP-SKU", 1, 100)));
                        OrderCreated created = restTemplate.postForObject(
                                selfOrdersUrl, request, OrderCreated.class);
                        if ((System.nanoTime() - t0) / 1_000_000 >= 250) {
                            slow.incrementAndGet();
                        }
                        if (created != null && created.id() != null) {
                            synchronized (ids) {
                                ids.add(created.id());
                            }
                        } else {
                            fail(failures, "нет id в ответе");
                        }
                    } catch (Exception e) {
                        fail(failures, e.getMessage());
                    } finally {
                        inFlight.release();
                    }
                });
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        pool.shutdown();
        try {
            if (!pool.awaitTermination(90, TimeUnit.SECONDS)) {
                log.warn("Warmup: прогрев-заказы не уложились в 90 с");
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        // Тихий отказ: 3000 исключений в лог — это шум; пишем первые 3 и итог.
        int failed = failures.get();
        if (failed > 0) {
            log.warn("Warmup: {} из {} заказов не создались; {} дольше 250 мс",
                    failed, ordersToSend, slow.get());
        } else if (slow.get() > 0) {
            log.info("Warmup: {} запросов дольше 250 мс (прогрев уловил медленный путь)",
                    slow.get());
        }
        return ids;
    }

    /** Первые неудачи — в warn (не более 3), остальные только в счётчик. */
    private static void fail(AtomicInteger failures, String message) {
        int n = failures.incrementAndGet();
        if (n <= 3) {
            log.warn("Warmup POST не удался ({}-я попытка): {}", n, message);
        }
    }

    /**
     * Ждёт, пока прогрев-заказы, чьи id удалось получить, выйдут из PENDING
     * (то есть прошли путь Kafka → Payment → транзакция статуса), либо
     * истечёт таймаут. Это ограниченная проверка готовности пайплайна и
     * попытка не допустить, чтобы прогрев-заказы попали в факты сценария.
     *
     * Проверка ОДНИМ EXISTS-запросом на опрос: 3000 заказов × GET каждые
     * 200 мс в отдельных SELECT'ах сами создали бы ложную нагрузку на БД.
     */
    private boolean awaitDrain(List<String> ids) {
        long deadline = System.currentTimeMillis() + DRAIN_TIMEOUT_MS;
        while (System.currentTimeMillis() < deadline) {
            boolean anyPending;
            synchronized (ids) {
                anyPending = !ids.isEmpty() && orderRepository.anyPending(ids);
            }
            if (!anyPending) {
                return true;
            }
            try {
                Thread.sleep(200);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
        log.warn("Warmup: дренаж не завершился за {} мс, остаются PENDING", DRAIN_TIMEOUT_MS);
        return false;
    }

    /**
     * Ждёт, пока события order.paid прогрев-заказов уйдут из outbox
     * в шину (EventsPublisher помечает строки sent_at).
     *
     * Отдельный, а не общий с заказами цикл: заказы могут быть оплачены
     * минуту назад, а событие ещё ждать своей пачки — см. javadoc
     * EVENT_DRAIN_TIMEOUT_MS, зачем мы этого ждём вообще.
     */
    private boolean awaitEventDrain() {
        long deadline = System.currentTimeMillis() + EVENT_DRAIN_TIMEOUT_MS;
        while (System.currentTimeMillis() < deadline) {
            if (outboxRepository.countUnsent("order.paid") == 0) {
                return true;
            }
            try {
                Thread.sleep(POLL_INTERVAL_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
        log.warn("Warmup: события order.paid не ушли за {} мс, рискуем попасть в факты S1",
                EVENT_DRAIN_TIMEOUT_MS);
        return false;
    }
}
