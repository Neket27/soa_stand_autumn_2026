package orders.flow;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;
import orders.api.dto.OrderPaidEvent;
import orders.store.OutboxRepository;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * HTTP-доставщик исходящих событий в шину событий (задание 2).
 *
 * === ЗАЧЕМ ОТДЕЛЬНЫЙ ОТ OutboxDispatcher ===
 *
 * OutboxDispatcher несёт события order.charge в Kafka. Шина курса —
 * это НЕ Kafka: contracts/events.yaml задаёт HTTP
 * POST $EVENTS_URL/events. Оба доставщика читают одну таблицу
 * outbox_events, но с разным фильтром event_type — две независимые
 * очереди (см. OutboxRepository.claimBatch).
 *
 * === КАК РАБОТАЕТ (тот же паттерн, что у Kafka-доставщика) ===
 *
 * 1. Тик каждые 200 мс: claim пачки ≤100 строк event_type = 'order.paid'.
 * 2. Пачка шлётся ПАРАЛЛЕЛЬНО (свой пул потоков): упавшая шина в S6
 *    держит каждый запрос до read timeout (3 с), и последовательная
 *    отправка 100 событий заняла бы 300 с вместо 3 с.
 * 3. Успешные (2xx) → markSent ОДНОЙ командой. Неуспешные → releaseClaim:
 *    строка возвращается в очередь СРАЗУ, а не через visibility timeout.
 *    Это критично для S6: раннер ждёт после подъёма шины всего 30 с.
 *
 * === AT-LEAST-ONCE И ОДИН id У КОПИЙ ===
 *
 * Падение между send и markSent (kill -9 в S7/S8 как раз попадает в
 * это окно) даёт повтор. Повтор шлёт ТОТ ЖЕ payload из БД — со всем тем
 * же id события, «запечённым» при записи в outbox (OrderPaidEvent) —
 * поэтому все копии одного события имеют одинаковый id, как требует
 * S6. Шина по этому id помечает копию дублем.
 *
 * === ТИК НЕ ЗАВИСАЕТ ===
 *
 * Весь тик ограничен get(timeout) на пачку: даже если шина висит,
 * тик вернётся через ~5 с, отпустит строки и продолжит. Никаких
 * неограниченных блокировок шедулера.
 */
@Slf4j
@Component
public class EventsPublisher {

    /** Размер пачки: баланс между round-trip'ами и параллелизмом. */
    private static final int BATCH_SIZE = 100;

    /**
     * Тик-таймаут строго больше read timeout клиента шины (3 с) + запас:
     * тик не должен обрываться, пока живые запросы ещё в полёте.
     */
    private static final Duration TICK_TIMEOUT = Duration.ofSeconds(5);

    /**
     * Visibility timeout claim'а.
     *
     * Живая реплика держит claim не дольше тика (~5 с, ограничен
     * TICK_TIMEOUT) и возвращает строки сама — releaseClaim. Значит 10 с
     * — щедрый запас в 2×. Короткое окно нужно для S7/S8: kill -9 может
     * случиться ПОСРЕДИ тика, между claim и release, и убитая реплика
     * уже не вернёт строки сама. С 60 с они пролежали бы в claim'е
     * почти минуту, а проверки S7/S8 идут через ~30–40 с после рестарта
     * — событие не успело бы уйти в шину. С 10 с строки пробуждаются
     * задолго до проверок (дубль безопасен: тот же payload, тот же id).
     */
    private static final Duration VISIBILITY_TIMEOUT = Duration.ofSeconds(10);

    /** Свой пул: шлём пачку параллельно, не трогая пул шедулера. */
    private final ExecutorService sender = Executors.newFixedThreadPool(16, r -> {
        Thread t = new Thread(r, "events-publisher");
        t.setDaemon(true);
        return t;
    });

    private final OutboxRepository outboxRepository;

    private final RestTemplate busRestTemplate;

    private final String eventsUrl;

    /**
     * Два RestTemplate-бина (общий и шины) → нужен @Qualifier, иначе
     * Spring не знает, какой внедрять. Конструктор явный, потому что
     * Lombok не переносит qualifier в генерируемый код.
     */
    public EventsPublisher(OutboxRepository outboxRepository,
                           @Qualifier("busRestTemplate") RestTemplate busRestTemplate,
                           @Value("${EVENTS_URL:http://mocks:8080}") String eventsUrl) {
        this.outboxRepository = outboxRepository;
        this.busRestTemplate = busRestTemplate;
        this.eventsUrl = eventsUrl;
    }

    /**
     * Тик поллинга: каждые 200 мс. fixedDelay — пачки не накладываются
     * (та же дисциплина, что у OutboxDispatcher).
     */
    @Scheduled(fixedDelay = 200)
    public void pump() {
        List<OutboxRepository.OutboxEvent> events =
                outboxRepository.claimBatch(OrderPaidEvent.TYPE, BATCH_SIZE, VISIBILITY_TIMEOUT);
        if (events.isEmpty()) {
            return;
        }
        List<Future<Integer>> futures = new ArrayList<>(events.size());
        for (OutboxRepository.OutboxEvent event : events) {
            futures.add(sender.submit(() -> post(event.payload())));
        }

        List<Long> sent = new ArrayList<>();
        List<Long> retry = new ArrayList<>();
        for (int i = 0; i < futures.size(); i++) {
            try {
                int code = futures.get(i).get(TICK_TIMEOUT.toMillis(), TimeUnit.MILLISECONDS);
                if (code >= 200 && code < 300) {
                    sent.add(events.get(i).id());
                } else {
                    retry.add(events.get(i).id());
                }
            } catch (TimeoutException | ExecutionException e) {
                // Таймаут тика или клиентская ошибка запроса — строка
                // должна вернуться в очередь, а не ждать visibility timeout.
                retry.add(events.get(i).id());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                retry.add(events.get(i).id());
                log.info("Events: отправка прервана, выходим из тика");
                break;
            }
        }
        outboxRepository.markSent(sent);
        outboxRepository.releaseClaim(retry);
        if (!retry.isEmpty()) {
            log.warn("Events: {} событий не доставлено (шина недоступна?), вернулись в очередь",
                    retry.size());
        }
    }

    /**
     * POST /events одной строки outbox. Возвращает HTTP-статус:
     * не бросает исключения — любой отказ обрабатывается вызывающим
     * кодом одинаково (retry), различать причины на месте не нужно.
     */
    private int post(String payload) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        try {
            ResponseEntity<String> response = busRestTemplate.exchange(
                    eventsUrl + "/events", HttpMethod.POST,
                    new HttpEntity<>(payload, headers), String.class);
            return response.getStatusCode().value();
        } catch (Exception e) {
            log.debug("Events: не удалось отправить событие: {}", e.getMessage());
            return 599; // «нет ответа» — любое значение вне 2xx
        }
    }
}
