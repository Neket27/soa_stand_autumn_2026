package orders.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;
import orders.api.dto.OrderCreated;
import orders.api.dto.OrderRequest;
import orders.inventory.InventoryClient;
import orders.store.OrderRepository;
import orders.store.OutboxRepository;
import orders.store.ReleaseDutyRepository;

import java.util.UUID;

/**
 * Сервис создания заказа (use case: create order).
 *
 * === СЦЕНАРИЙ РАБОТЫ ===
 * 1. Валидация запроса — на слое API (аннотации на DTO + @Valid)
 * 2. Резервируем товары в Inventory API
 * 3. Создаём заказ в PENDING
 * 4. Пишем событие оплаты в outbox (Transactional Outbox)
 * 5. Оплату выполнит Consumer (KafkaChargeDispatcher), НЕ этот сервис
 *
 * === ПОЧЕМУ OUTBOX, А НЕ ПРЯМАЯ ОТПРАВКА В KAFKA ===
 *
 * Если отправить событие в Kafka напрямую (или в afterCommit), получим
 * dual-write: транзакция БД закоммичена, send() упал → событие потеряно,
 * заказ навсегда PENDING. Откатить БД после коммита нельзя.
 *
 * С outbox событие пишется В ТОЙ ЖЕ транзакции, что и заказ:
 *   — коммит прошёл → заказ и событие в БД, отправит OutboxDispatcher;
 *   — откат → нет ни заказа, ни события, ничего «висящего».
 * Это и есть гарантия атомарности «заказ + событие».
 *
 * Оплата здесь принципиально асинхронна: POST /orders возвращает
 * pending, не дожидаясь Payment. Так заказ не зависит от времени
 * ответа Payment и переживает его падение (retry в Consumer + Reaper).
 *
 * === ГРАНИЦЫ ТРАНЗАКЦИИ И КОМПЕНСАЦИЯ РЕЗЕРВА ===
 *
 * reserve() — REST-вызов, он НЕ в транзакции: транзакция длиной в
 * внешний вызов держала бы соединение из пула Hikari (10 штук) до 15 с
 * и при медленном Inventory опустошила бы пул. Транзакция обнимает
 * только два INSERT'а (заказ + событие).
 *
 * Но раз резерв — вне транзакции, его нельзя откатить вместе с заказом:
 * при откате заказа резерв превратился бы в вечную утечку (в заглушке
 * нет TTL, Reaper ходит только по таблице orders). Поэтому откат
 * компенсируется записью в журнал release_duties, а снятие резерва
 * добивает Reaper. Сети в компенсации нет — упавший запрос отвечает
 * быстро, сеть это делает фоновый шедулер.
 */
@Slf4j
@Service
public class CreateOrderService {

    private final InventoryClient inventoryClient;
    private final OrderRepository orderRepository;
    private final OutboxRepository outboxRepository;
    private final ReleaseDutyRepository releaseDutyRepository;

    /** Обычная транзакция: заказ + событие outbox атомарны. */
    private final TransactionTemplate transactionTemplate;

    /**
     * Отдельная транзакция для журнала компенсаций: запись обязана
     * пережить откат основной транзакции. Если писать её в той же
     * (пропагация REQUIRED), строка журнала уедет вместе с откатом.
     *
     * REQUIRES_NEW — самое тонкое место.
     * catch исполняется внутри ещё живой (но обречённой) транзакции.
     * Если писать журнал с обычной пропагацией (REQUIRED), строка release_duties
     * присоединилась бы к этой транзакции и уехала вместе с её откатом — журнал был бы пуст
     * ровно в те моменты, когда он нужен. Поэтому  задаётся второй TransactionTemplate с PROPAGATION_REQUIRES_NEW
     */
    private final TransactionTemplate requiresNewTransaction;

    public CreateOrderService(InventoryClient inventoryClient,
                              OrderRepository orderRepository,
                              OutboxRepository outboxRepository,
                              ReleaseDutyRepository releaseDutyRepository,
                              PlatformTransactionManager transactionManager) {
        this.inventoryClient = inventoryClient;
        this.orderRepository = orderRepository;
        this.outboxRepository = outboxRepository;
        this.releaseDutyRepository = releaseDutyRepository;
        this.transactionTemplate = new TransactionTemplate(transactionManager);
        this.requiresNewTransaction = new TransactionTemplate(transactionManager);
        this.requiresNewTransaction.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
    }

    public OrderCreated create(OrderRequest request) {
        String orderId = UUID.randomUUID().toString();
        long amountCents = request.items().stream()
                .mapToLong(i -> i.qty() * i.priceCents())
                .sum();

        // Резерв — ВНЕ транзакции: висящий Inventory не должен держать
        // соединение из пула (10 штук)
        long t0 = System.nanoTime();
        inventoryClient.reserve(orderId, request.items());
        long t1 = System.nanoTime();

        try {
            transactionTemplate.executeWithoutResult(status -> {
                orderRepository.insertPending(orderId, request.userId(), amountCents, request.items());
                outboxRepository.insert(orderId, "order.charge", orderId);
            });
        } catch (RuntimeException e) {
            recordReleaseDuty(orderId, e);
            throw e;
        }
        long t2 = System.nanoTime();

        // Диагностика хвоста латентности (стресс 1000/с): если POST
        // обрабатывается дольше 250 мс, пишем, ГДЕ именно ушло время —
        // внешний вызов reserve() к Inventory или транзакция с двумя
        // INSERT'ами. Работает в паре с SlowRequestFilter.
        long totalMs = (t2 - t0) / 1_000_000;
        if (totalMs >= 250) {
            log.warn("SLOW create {}: reserve {} мс, tx {} мс (итого {} мс)",
                    orderId, (t1 - t0) / 1_000_000, (t2 - t1) / 1_000_000, totalMs);
        }

        return new OrderCreated(orderId, "pending");
    }

    /**
     * Компенсация шага «резерв», если заказ создать не удалось.
     *
     * Только фиксируем обязанность в журнале release_duties — никакой
     * сети: запрос уже упал, экономим время ответа (бюджет 1 с на POST),
     * а DELETE выполнит Reaper на ближайшем тике. DELETE идемпотентен,
     * так что повторы безопасны.
     *
     * Сбой записи в журнал (БД лежит — типичная причина отката заказа)
     * не маскируем: главная ошибка важнее, но фиксируем громко — это
     * единственный случай, когда резерв требует ручного снятия.
     *
     * === ДОПУЩЕНИЕ ===
     *
     * Исхожу из того, что БД жива и отдаёт результат: компенсация
     * пишется в ту же БД, что и заказ (только в отдельной транзакции).
     * Полный отказ БД здесь НЕ обрабатывается — запись журнала упадёт
     * вместе с ней, и резерв утечёт. Это осознанный компромисс: закрыть
     * остаток можно лишь механизмом вне нашей БД (TTL на резервах на
     * стороне Inventory), который на стенде недоступен.
     */
    private void recordReleaseDuty(String orderId, RuntimeException cause) {
        try {
            requiresNewTransaction.executeWithoutResult(status ->
                    releaseDutyRepository.insert(orderId));
            log.warn("Заказ {} не создан ({}), резерв записан в release_duties, снимет Reaper",
                    orderId, cause.getMessage());
        } catch (Exception e) {
            log.error("УТЕЧКА РЕЗЕРВА: заказ {} не создан ({}), и записать в release_duties не удалось. "
                    + "Сними вручную: DELETE /reservations/{}", orderId, cause.getMessage(), orderId, e);
        }
    }

    /*
     * === КАК ЗАКРЫТЬ УТЕЧКУ РЕЗЕРВА ПОЛНОСТЬЮ: TTL НА РЕЗЕРВАХ ===
     *
     * release_duties закрывает все случаи, кроме полного отказа БД
     * Orders (см. «ДОПУЩЕНИЕ» выше). Каноничное решение всей проблемы —
     * TTL (time-to-live) на стороне Inventory: резерв создаётся с
     * дедлайном, и Inventory сам его освобождает, если дедлайн истёк.
     *
     * === МЕХАНИКА ===
     *
     * Сейчас резерв — это запись-«обязательство», которое кто-то
     * обязан освободить. Если тот, кто обязан (Orders), умер —
     * обязательство висит вечно. С дедлайном резерв перестаёт быть
     * чьей-то обязанностью и становится истекающим состоянием:
     *
     *   POST /reservations → резерв "held", expires_at = now + 15 мин
     *                               │
     *      ┌────────────────────────┼────────────────────────┐
     *  заказ создан            заказ НЕ создался         Orders умер целиком
     *  подтверждает             (наш кейс)                между reserve
     *  резерв (снимает              │                      и insertPending
     *  дедлайн)                     │                          │
     *      │                   никто не подтверждает     никто не подтверждает
     *      ▼                       ▼                          ▼
     *  резерв живёт           Inventory через 15 мин     Inventory через 15 мин
     *  как положено           сам снимает резерв         сам снимает резерв
     *
     * Ключевое: механизм освобождения живёт ВНУТРИ Inventory — в той же
     * службе, что и резерв. Ему не нужна Orders, её БД, её логи, её
     * Reaper. Сбойные домены не пересекаются: утечка самовылечивается
     * за ≤15 минут в любом раскладе, включая «БД Orders легла между
     * reserve() и insertPending» — тот единственный кейс, который
     * release_duties не закрывает.
     *
     * === ПОЧЕМУ «ПОДТВЕРЖДЕНИЕ», А НЕ ГОЛЫЙ TTL ===
     *
     * Чистый TTL «через 15 мин снять всё» был бы неправильным: заказ
     * мог успеть оплатиться, а Inventory снял бы резерв у оплаченного
     * заказа (нарушение «paid ⇒ зарезервировано»). Поэтому дедлайн
     * снимается подтверждением: Orders подтверждает резерв сразу после
     * создания заказа (или резерв продлевается, пока заказ жив). Наш
     * утечный кейс — это ровно «подтверждение никогда не пришло».
     *
     * Это тот же принцип, что в аренде переговорки (бронь истекает,
     * если не подтвердили), в распределённых локах (lease) и в TCP:
     * не «кто-то должен уборку сделать», а «само протухает».
     *
     * На стенде механизм недоступен: mocks/ — read-only код курса,
     * добавить TTL в заглушку Inventory нельзя. Поэтому здесь —
     * журнальный компромисс release_duties + Reaper.
     */
}
