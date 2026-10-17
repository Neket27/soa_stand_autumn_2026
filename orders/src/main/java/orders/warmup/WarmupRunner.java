package orders.warmup;

import lombok.extern.slf4j.Slf4j;
import orders.api.dto.Item;
import orders.api.dto.OrderCreated;
import orders.api.dto.OrderRequest;
import orders.store.OrderRepository;
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
 * === ЗАЧЕМ (найдено стресс-тестом на 1000 заказов/с) ===
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
 *   - 5 × 12 коньюмеров входят в consumer group Kafka и догоняют
 *     outbox — конкуренция за CPU/БД как раз в первые секунды.
 * Раннер (runner/run.go) ждёт GET /healthz = 200 и тут же начинает
 * слать 1000 заказов/с — то есть стартует РОВНО на холоде.
 *
 * === КАК УСТРОЕН ПРОГРЕВ ===
 *
 * ApplicationRunner выполняется ПОСЛЕ старта Tomcat (порт уже слушается)
 * и ДО статуса ready. Пока прогрев не закончен, HealthController отвечает
 * 503 — раннер продолжает ждать и получает сервис «тёплым»:
 *   1. N POST /orders в себя же (через общий RestTemplate): прогревает
 *      Tomcat-потоки, валидацию, Jackson, контроллер, reserve() через
 *      пул соединений, транзакцию с двумя INSERT'ами, outbox, и оплату
 *      коньюмерами через Kafka (точный боевой путь от и до);
 *   2. дожидается выхода прогрев-заказов из PENDING (timeout-защита):
 *      к моменту /healthz=200 пайплайн Kafka→Payment→БД доказан
 *      работоспособным, а заказы уже оплачены.
 *
 * === ПОЧЕМУ ПРОГРЕВ НЕ ЛОМАЕТ ПРОВЕРКИ ===
 *
 * Раннер вызывает POST /admin/reset в заглушках в НАЧАЛЕ каждого
 * сценария — факты прогрева (резервы, списания) стираются до того,
 * как проверки их читают. Прогрев-заказы к тому моменту уже оплачены
 * (см. дренаж выше), поэтому в факты сценариев они не попадают.
 */
@Slf4j
@Component
public class WarmupRunner implements ApplicationRunner {

    /**
     * Сколько заказов создаём на прогреве на РЕПЛИКУ при ХОЛОДНОМ старте
     * кластера (всего по стенду — ×5). Цель — прогреть не «сам код», а
     * систему НА РЕАЛЬНОМ ТЕМПЕ.
     *
     * Почему мало — не работает: раньше было 120 заказов (~220/с), а
     * реальный прогон стартует с 0 до 1000 заказов/с на реплику МГНОВЕННО.
     * Стресс-тест 5000/с: хвост целиком в первые ~1,5 с после старта
     * отправки (Tomcat раскручивает потоки с 5 до 168, коньюмеры получают
     * первую волну событий, Postgres входит в полный темп записи) — и
     * всё это на «непрогретом темпе». 3000 заказов по 2000/с — темп
     * выше боевого, поэтому боевой старт для системы становится
     * «продолжением разгона», а не первым контактом.
     */
    private static final int WARMUP_ORDERS_COLD = 3000;

    /**
     * Прогрев при РЕСТАРТЕ сервиса на живой БД (сценарии S6/S7 перезапускают
     * Orders посреди прогона): код тот же, а вот «входить в боевой темп»
     * заново не нужно — резкий прогрев посреди работающего сценария сам
     * стал бы источником ложной нагрузки и чужих фактов. 120 заказов —
     * прежний, проверенный сценариями объём (JVM-старт + связки).
     */
    private static final int WARMUP_ORDERS_RESTART = 120;

    /** Темп прогрева на реплику, заказов/с (боевой — 1000/с при 5000/с по стенду). */
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
    private final WarmupBarrierRepository warmupBarrierRepository;
    private final String selfOrdersUrl;
    private final int expectedPeers;

    public WarmupRunner(RestTemplate restTemplate,
                        OrderRepository orderRepository,
                        WarmupBarrierRepository warmupBarrierRepository,
                        @Value("${PORT:8080}") int port,
                        @Value("${WARMUP_BARRIER_EXPECTED:5}") int expectedPeers) {
        this.restTemplate = restTemplate;
        this.orderRepository = orderRepository;
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
        joinBarrier();

        ready = true;
        long totalMs = (System.nanoTime() - t0) / 1_000_000;
        log.info("Warmup: {} заказов отправлено за {} мс ({}), дренаж {} (итого {} мс)",
                ids.size(), sentMs, coldBoot ? "холодный старт" : "рестарт",
                drained ? "ok" : "TIMEOUT " + DRAIN_TIMEOUT_MS + " мс", totalMs);
    }

    /**
     * Барьер готовности: помечаем реплику и ждём, пока пометятся ВСЕ.
     *
     * Раннер стартует сценарий от /healthz=200 первой попавшейся реплики —
     * без барьера S1 начинался, пока остальные реплики ещё дренировали
     * прогрев, и их прогрев-заказы попадали в факты сценария (TASK=1:
     * 4624 чужих списания, проверка «paid без резерва» — FAIL). После
     * барьера чужих pending не существует в принципе: /healthz=200
     * означает «весь кластер прогрет и дренирован».
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

    /** Отправляет прогрев-заказы в себя же с боевым темпом, возвращает их id. */
    private List<String> sendWarmupOrders(int ordersToSend) {
        List<String> ids = new ArrayList<>();
        AtomicInteger failures = new AtomicInteger();
        AtomicInteger slow = new AtomicInteger();
        ExecutorService pool = Executors.newFixedThreadPool(MAX_IN_FLIGHT);
        Semaphore inFlight = new Semaphore(MAX_IN_FLIGHT);
        long start = System.nanoTime();
        try {
            for (int i = 0; i < ordersToSend; i++) {
                // Ровный темп WARMUP_RATE_PER_S, как у боевого раннера:
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
     * Ждёт, пока все прогрев-заказы выйдут из PENDING (то есть прошли
     * весь путь Kafka → Payment → транзакция статуса). Это и доказательство
     * готовности пайплайна, и страховка от того, что прогрев-заказы
     * «доживут» до сценария и попадут в его факты.
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
}
