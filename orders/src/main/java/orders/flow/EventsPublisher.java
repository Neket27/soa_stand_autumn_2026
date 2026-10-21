package orders.flow;

import jakarta.annotation.PreDestroy;
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
 * 2. Пачка шлётся ПАРАЛЛЕЛЬНО — по виртуальному потоку на событие:
 *    упавшая шина в S6 держит каждый запрос до read timeout (3 с), и
 *    последовательная отправка 100 событий заняла бы 300 с вместо 3 с.
 *    Потолок одновременных запросов при этом задаёт НАШ пул соединений
 *    к шине (32, RestTemplateConfig.busRestTemplate), а не число потоков.
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
 * Весь тик ограничен ОДНИМ дедлайном (TICK_TIMEOUT) на всю пачку: даже
 * если шина висит, цикл чтения результатов вернётся не позже чем через
 * ~5 с (обычно ~3 с — по самому медленному запросу, при живой шине —
 * за миллисекунды), отпустит строки в очередь и продолжит. Никаких
 * неограниченных блокировок шедулера. Сверх дедлайна каждый запрос
 * дополнительно ограничен read timeout (3 с), так что и фоновые
 * виртуальные потоки живут недолго.
 */
@Slf4j
@Component
public class EventsPublisher {

    /** Размер пачки: баланс между round-trip'ами и параллелизмом. */
    private static final int BATCH_SIZE = 100;

    /**
     * Дедлайн ВСЕГО тика — одна граница на всю пачку, а не таймаут на
     * каждый Future отдельно.
     *
     * Больше read timeout клиента шины (3 с) + запас: тик не должен
     * обрываться, пока запросы, отправленные в первые же миллисекунды,
     * ещё в полёте. При упавшей шине вся пачка укладывается в эти 5 с:
     * 32 запроса висят до read timeout (3 с), остальные упрутся в
     * connectionRequestTimeout (2 с) и вернут «нет ответа» раньше.
     *
     * 5 с — ВЕРХНЯЯ ГРАНИЦА, а не рабочее ожидание: цикл выходит сразу,
     * когда закрылись все Future (обычно ~3 с — по самому медленному
     * запросу, а при живой шине — за миллисекунды). Дедлайн срабатывает
     * только в худшем случае. Поэтому граница и не может быть 3 с:
     * она обязана перекрывать read timeout плюс накладные (claim из БД,
     * запуск 100 задач), иначе оборвала бы запросы как раз перед ответом.
     *
     * ЛОВУШКА, из-за которой здесь дедлайн, а не таймаут на каждую строку:
     * get(5 с) НА КАЖДЫЙ Future не ограничивал СУММУ ожиданий цикла — тик
     * складывался из них подряд. При упавшей шине старый код шёл волнами
     * по размеру пула отправителя:
     *
     *   ceil(пачка / потоки) × read timeout = ceil(100 / 16) × 3 с = 21 с
     *
     * То есть обещание «тик вернётся через 5 с» нарушалось в ~4 раза
     * (таймаут на Future тут ни при чём: пауза волны 3 с меньше его 5 с,
     * срабатывал бы он лишь при волнах дольше 5 с). Теперь цикл ограничен
     * ровно TICK_TIMEOUT: остаток дедлайна вычитается у каждого Future,
     * и любое число строк укладывается в одну границу.
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

    /**
     * Отправитель пачки: по одному виртуальному потоку на событие.
     *
     * === ПОЧЕМУ ВИРТУАЛЬНЫЕ, А НЕ ФИКСИРОВАННЫЙ ПУЛ ===
     *
     * Задача — чистый blocking I/O: HTTP POST с read timeout 3 с, ни
     * одного монитора на время ожидания сокета и никакой CPU-работы.
     * Это ровно тот профиль, ради которого виртуальные потоки и
     * сделаны: поток живёт, пока ждёт сеть, и стоит копейки, поэтому
     * его количество ничем кроме полезной работы ограничивать не нужно.
     *
     * Прежний пул из 16 платформенных потоков ставил ПОТОЛОК там, где
     * он уже задан другим местом — пулом соединений busRestTemplate (32,
     * см. RestTemplateConfig.busRestTemplate): пачка шла волнами по 16,
     * в полёте было 16 запросов из возможных 32, а оставшиеся события
     * просто ждали в очереди пула. Теперь все задачи стартуют сразу,
     * а одновременных запросов столько, сколько выдерживает пул
     * соединений — один естественный предел вместо двух конкурирующих.
     *
     * === ЧТО ЭТА ЗАМЕНА НЕ ДАЁТ ===
     *
     * Параллелизма больше 32 всё равно не будет: 32 — это НАША ЖЕ
     * константа maxTotal/defaultMaxPerRoute в RestTemplateConfig, а не
     * потолок шины (у заглушки лимита соединений нет) и не потолок
     * потоков. Поднять параллелизм — значит поднять эти цифры. Остальные
     * события упрутся в connectionRequestTimeout (2 с), вернут «нет
     * ответа» и уйдут в retry — та самая задуманная схема «копятся
     * в outbox → уходят сразу после подъёма шины» (S6).
     *
     * === ПИННИНГ (ограничение Java 21) ===
     *
     * В JDK до 24 монитор (synchronized), удерживаемый во время I/O,
     * «прибивал» carrier-поток, и параллелизм молча падал бы до числа
     * ядер. На нашем пути (Apache HttpClient 5 + RestTemplate) мониторов
     * на время ожидания сокета нет — в исторических прогонах S6–S8 после
     * перехода на виртуальные потоки пачки доходили до полного параллелизма
     * пула соединений без наблюдавшихся просадок.
     *
     * === ЖИЗНЕННЫЙ ЦИКЛ ===
     *
     * Виртуальные потоки не держат JVM (уходящих не надо помечать
     * daemon, как было раньше), но именование задаём явно — иначе в
     * thread dump'е они будут неразличимы. Пул закрывается в shutdown().
     */
    private final ExecutorService sender = Executors.newThreadPerTaskExecutor(
            Thread.ofVirtual().name("events-publisher-", 0).factory());

    private final OutboxRepository outboxRepository;

    private final RestTemplate busRestTemplate;

    private final String eventsUrl;

    /**
     * Остановка пачки при закрытии контекста: прерываем то, что в
     * полёте, и не даём принять новые задачи.
     *
     * Ждать завершения незачем: каждый запрос ограничен read timeout
     * 3 с, а прерванный POST шина может принять уже после остановки —
     * это дубль с ТЕМ ЖЕ id события, который шина отбрасывает
     * (at-least-once, один id у всех копий). Незакоммиченные строки
     * при этом вернутся не через releaseClaim (его в этот момент уже не
     * будет), а через visibility timeout claim'а — после рестарта.
     */
    @PreDestroy
    public void shutdown() {
        sender.shutdownNow();
    }

    /**
     * Два RestTemplate-бина (общий и шины) → нужен @Qualifier, иначе
     * Spring не знает, какой внедрять.
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

        if (events.isEmpty())
            return;

        List<Future<Integer>> futures = new ArrayList<>(events.size());
        for (OutboxRepository.OutboxEvent event : events)
            futures.add(sender.submit(() -> post(event.payload())));

        List<Long> sent = new ArrayList<>();
        List<Long> retry = new ArrayList<>();
        // Один дедлайн на ВЕСЬ тик: остаток (deadline - сейчас) вычитается
        // у каждого Future, поэтому суммарное ожидание цикла при упавшей
        // шине не превысит TICK_TIMEOUT ни при каком раскладе. Таймаут же
        // на каждый Future отдельно (как было) давал ceil(100/16) × 3 с
        // ≈ 21 с на пачку: волны пула ждали друг друга подряд.
        long deadline = System.nanoTime() + TICK_TIMEOUT.toNanos();

        for (int i = 0; i < futures.size(); i++) {
            long remainingNanos = deadline - System.nanoTime();
            if (remainingNanos <= 0) {
                // Дедлайн исчерпан: хвост пачки не ждём, разбираем по уже
                // готовым результатам и возвращаем в очередь
                retryRest(i, futures, events, sent, retry);
                break;
            }
            try {
                int code = futures.get(i).get(remainingNanos, TimeUnit.NANOSECONDS);
                if (code >= 200 && code < 300) {
                    sent.add(events.get(i).id());
                } else {
                    retry.add(events.get(i).id());
                }
            } catch (TimeoutException e) {
                // Дедлайн дождался этой строки: её и весь остаток пачки —
                // в очередь, а не ждать visibility timeout.
                retryRest(i, futures, events, sent, retry);
                break;
            } catch (ExecutionException e) {
                // Исключение, которое post() не поглотил (там ловится всё,
                // что относится к запросу). Строка идёт в очередь: ошибка
                // не разбирается на месте — «доставлено / не доставлено»
                // здесь решает только 2xx.
                retry.add(events.get(i).id());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                retryRest(i, futures, events, sent, retry);
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
     * Разбор хвоста пачки (от строки from и до конца) при исчерпании
     * дедлайна или прерывании тика.
     *
     * Уже ЗАВЕРШИВШИЕСЯ запросы забираем по результату, даже если очередь
     * чтения до них не дошла: успешную отправку нельзя превращать в лишний
     * повтор, а неуспешная и так пойдёт в retry. Отмена остальных — best
     * effort: у неначавшейся задачи запрос не случится вовсе, а у уже
     * идущего обрыв сокета interrupt не отменяет — он завершится сам по
     * read timeout (3 с). То есть cancel здесь прежде всего означает «не
     * ждать результат», а не «обрвать сеть».
     *
     * Если POST успел уйти в сеть уже после releaseClaim — это дубль с тем
     * же id события: копию примет и отправит следующий тик, а шина
     * отбросит повтор (at-least-once, один id у всех копий — см. джавадок
     * класса). Поэтому и отмена, и «не успел прочитать» безопасны.
     */
    private void retryRest(int from, List<Future<Integer>> futures,
                           List<OutboxRepository.OutboxEvent> events,
                           List<Long> sent, List<Long> retry) {
        for (int i = from; i < futures.size(); i++) {
            Future<Integer> future = futures.get(i);
            Long id = events.get(i).id();
            if (future.isDone() && !future.isCancelled()) {
                // isDone → get() возвращается мгновенно, без ожидания
                try {
                    int code = future.get();
                    if (code >= 200 && code < 300) {
                        sent.add(id);
                        continue;
                    }
                } catch (Exception e) {
                    // задача упала → в retry, как и любой не-2xx
                }
            } else {
                future.cancel(true);
            }
            retry.add(id);
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
