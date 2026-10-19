package orders.config;

import org.apache.kafka.clients.admin.AdminClient;
import org.apache.kafka.clients.admin.AdminClientConfig;
import org.apache.kafka.clients.admin.NewTopic;
import org.apache.kafka.clients.consumer.ConsumerConfig;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.apache.kafka.common.serialization.StringSerializer;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.annotation.EnableKafka;
import org.springframework.kafka.core.*;
import org.springframework.kafka.config.ConcurrentKafkaListenerContainerFactory;
import org.springframework.kafka.listener.ContainerProperties;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.util.backoff.FixedBackOff;

import java.util.HashMap;
import java.util.Map;

/**
 * Конфигурация Kafka для асинхронной обработки платежей.
 *
 * === ЧТО ЗДЕСЬ ПРОИСХОДИТ ===
 * Kafka используется как шина событий: когда заказ создан и зарезервирован,
 * он не оплачивается сразу, а отправляется в Kafka. Consumer забирает заказ
 * из топика и вызывает Payment API.
 *
 * === КАК ЭТО РАБОТАЕТ ===
 * 1. Producer отправляет событие "OrderCreated" в топик "orders-charge"
 * 2. Consumer читает событие и вызывает Payment.charge()
 * 3. При успехе — заказ переходит в PAID
 * 4. При отказе (402) — заказ переходит в REJECTED, резерв снимается
 * 5. При ошибке (500) — событие возвращается в топик для retry
 *
 * === ПОЧЕМУ KAFKA, А НЕ ПРЯМОЙ ВЫЗОВ ===
 * — Надёжность: если Payment упадёт, события не потеряются
 * — Масштабирование: можно запустить несколько consumer-реплик
 * — Развязка: Orders не знает о Payment, только пишет в топик
 *
 * === КОНТРАКТ С ЗАГЛУШКАМИ (что проверяет раннер) ===
 * S1: Payment отклоняет 10% платежей (402) → заказ REJECTED, резерв снят
 * S2: Payment возвращает 500 на 33% запросов → событие ретраится,
 *     после успешного повтора заказ переходит в PAID
 */
@Configuration
@EnableKafka
public class KafkaConfig {

    @Value("${KAFKA_BOOTSTRAP_SERVERS:localhost:9092}")
    private String bootstrapServers;

    @Value("${KAFKA_GROUP_ID:orders-charge}")
    private String groupId;

    /**
     * Создание топика, если он ещё не существует.
     * Это нужно, чтобы Producer и Consumer могли работать
     * даже при первом запуске.
     */
    @Bean
    public AdminClient adminClient() {
        Map<String, Object> configs = new HashMap<>();
        configs.put(AdminClientConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        return AdminClient.create(configs);
    }

    // 12 партиций: charge() у Payment может отвечать секундами (сценарий S3 —
    // latency 2000 мс), и без параллелизма 20 оплат идут последовательно
    // (40 с > wait_s=30 раннера — S3 падает). 12 консюмер-потоков на 12
    // партициях делают их параллельно (~4 с).
    @Bean
    public NewTopic ordersChargeTopic() {
        return new NewTopic("orders-charge", 12, (short) 1);
    }

    /**
     * Producer — отправляет события о заказах в Kafka.
     * Используется OutboxDispatcher (Transactional Outbox).
     */
    @Bean
    public ProducerFactory<String, String> producerFactory() {
        Map<String, Object> props = new HashMap<>();
        props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        props.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        // Важно: ждём подтверждения от всех реплик
        props.put(ProducerConfig.ACKS_CONFIG, "all");

        // === ВНУТРЕННИЕ ретраи одного send() при сетевых сбоях ===
        // Что делает: повторяет ОДНУ отправку при восстанавливаемых ошибках
        // (перевыбор лидера, обрыв TCP, не хватает реплик). Это не ретраи
        // бизнес-логики — только сетевой уровень.
        //
        // Гарантии НЕ даёт: если попытки исчерпаны (или истёк
        // delivery.timeout.ms = 120 с по умолчанию), future падает
        // с исключением → get() в OutboxDispatcher бросает.
        //
        // Гарантию доставки даёт OUTBOX, а не эта строка: при ошибке
        // send() строка outbox_events остаётся с sent_at IS NULL,
        // и OutboxDispatcher повторит отправку на следующем тике
        // (200 мс) — и так бессконечно, пока Kafka не оживёт.
        //
        // Kafka-клиентах 3.x дефолт retries = Integer.MAX_VALUE.
        // 3 — это НЕ усиление надёжности, а быстрый отказ, чтобы
        // поллер не висел до 120 с на недоступной Kafka, а шёл дальше.
        // Без слоя 2 (outbox) эта строчка была бы единственной защитой —
        // и её было бы недостаточно.
        props.put(ProducerConfig.RETRIES_CONFIG, 3);

        // delivery.timeout.ms — верхняя граница жизни send() целиком
        // (все повторы + ожидание ack). Явно фиксируем, чтобы поведение
        // не зависело от дефолтов версии клиента.
        //
        // ЛОВУШКА: у Kafka строгое правило валидации
        //   delivery.timeout.ms >= linger.ms + request.timeout.ms
        // Дефолт request.timeout.ms = 30000, поэтому delivery.timeout = 10000
        // без снижения request.timeout — невалидная конфигурация: продюсер
        // НЕ СОЗДАЁТСЯ ("Failed to construct kafka producer"), каждый send()
        // падает сразу, и событие уходит только через Reaper. Поэтому ниже0

        // явно ставим request.timeout.ms = 5000.
        props.put(ProducerConfig.REQUEST_TIMEOUT_MS_CONFIG, 5_000);
        props.put(ProducerConfig.DELIVERY_TIMEOUT_MS_CONFIG, 10_000);

        // max.block.ms — вторая, «верхняя» граница send(). Ограничивает
        // период ДО создания future: ожидание метаданных (брокеры лежат,
        // bootstrap недоступен) и ожидание освобождения буфера
        // (buffer.memory). delivery.timeout.ms сюда НЕ входит — будущего
        // (future) в этот момент ещё не существует.
        //
        // Дефолт 60 с: при упавшей Kafka каждый тик OutboxDispatcher
        // висел бы минуту на первом же send(). Ставим 5 с — поллер
        // быстро откатывается к следующему тику, outbox копит события.
        props.put(ProducerConfig.MAX_BLOCK_MS_CONFIG, 5_000);
        return new DefaultKafkaProducerFactory<>(props);
    }

    @Bean
    public KafkaTemplate<String, String> kafkaTemplate() {
        return new KafkaTemplate<>(producerFactory());
    }

    /**
     * Consumer — читает события из топика и вызывает Payment API.
     * ContainerProperties.AckMode.MANUAL — коммитим offset вручную
     * только после успешной обработки.
     */
    @Bean
    public ConsumerFactory<String, String> consumerFactory() {
        Map<String, Object> props = new HashMap<>();
        props.put(ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        props.put(ConsumerConfig.GROUP_ID_CONFIG, groupId);
        props.put(ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class);
        props.put(ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class);
        // earliest: без закоммиченного offset'а (холодный старт, потеря БД Kafka)
        // читаем с начала топика — charge-события не должны пропускаться, если
        // они произведены до того, как группа собралась. Дубли безопасны:
        // consumer идемпотентен (проверка статуса + Idempotency-Key).
        // Это же важно и для тестов: они шлют события до старта consumer'а.
        props.put(ConsumerConfig.AUTO_OFFSET_RESET_CONFIG, "earliest");
        // Коммитим offset вручную (AckMode.MANUAL)
        props.put(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, false);
        return new DefaultKafkaConsumerFactory<>(props);
    }

    /**
     * Фабрика слушателей для @KafkaListener (имя бина — стандартное,
     * Spring Kafka ищет именно kafkaListenerContainerFactory).
     *
     * Здесь же настроен ретрай неудачных событий. Если бы мы оставили
     * дефолт, DefaultErrorHandler повторял бы чтение СРАЗУ и БЕСКОНЕЧНО
     * (FixedBackOff(0, 9)): при недоступном Payment коньюмер долбил бы
     * его без пауз, сжигая бюджет запросов (S4: не больше 30 запросов
     * за время отказа).
     *
     * FixedBackOff(1 с, 35 повторов): упавшая запись в seek-to-current
     * возвращается ПЕРВОЙ в свою партицию и блокирует очередь за собой,
     * поэтому цена паузы критична: с 5 с один заказ с цепочкой 500-х
     * держал свою партицию ~30 с и события за ним не доходили до
     * Payment за окно сценария (S2: 2 заказа с нулём запросов). С 1 с
     * цена попытки минимальна.
     *
     * Число попыток (35 ≈ 35 с) согласовано с паузой Circuit Breaker
     * (PAYMENT_BREAKER_WAIT_S=25): пока цепь в OPEN, попытка — быстрый
     * отказ без сети, так что длинное окно ретраев бесплатно по бюджету
     * запросов. Зато оно перекрывает время до пробного запроса HALF_OPEN
     * после подъёма Payment: заказ после чёрной дыры оплачивается
     * коньюмером, а не ждёт Reaper'а (порог 30 с) и не срывается к
     * концу сценария. Если попытки всё же исчерпаны, событие
     * пропускается (offset коммитится), заказ остаётся PENDING — его
     * подберёт Reaper, так что потеря невозможна.
     *
     * Дальнейший уровень (если нужен рост) — неблокирующие ретраи через
     * retry topic: падающая запись уходит в отдельный топик и вообще не
     * задерживает основную очередь.
     */
    @Bean
    public ConcurrentKafkaListenerContainerFactory<String, String> kafkaListenerContainerFactory() {
        ConcurrentKafkaListenerContainerFactory<String, String> factory =
                new ConcurrentKafkaListenerContainerFactory<>();
        factory.setConsumerFactory(consumerFactory());
        factory.getContainerProperties().setAckMode(ContainerProperties.AckMode.MANUAL);
        factory.setCommonErrorHandler(new DefaultErrorHandler(new FixedBackOff(1_000L, 35L)));
        return factory;
    }

    /*
     * =====================================================================
     * ДВА РЕТРАЯ: ГДЕ ЖИВЁТ КАЖДЫЙ И СКОЛЬКО ОНИ СТОЯТ (вопрос на защите)
     * =====================================================================
     *
     * RETRIES=3 (producer) и FixedBackOff(1 с, 35) (consumer) — это два
     * РАЗНЫХ ретрая на разных сторонах Kafka. Они не конкурируют и не
     * складываются: они последовательны и отвечают на разные вопросы
     * в разные моменты жизни одного события.
     *
     *   outbox_events --> [Producer: send()] --> топик --> [Consumer] --> Payment
     *                      RETRIES=3                     FixedBackOff(1с, 35)
     *                      «доставить в Kafka»           «обработать то, что уже в Kafka»
     *
     *
     * ---------------------------------------------------------------------
     * 1. СКОЛЬКО ЖИВЁТ «3 ПОПЫТКИ» ПРИ МЁРТВОЙ KAFKA
     * ---------------------------------------------------------------------
     *
     * Само retries=3 — счётчик ПОПЫТОК, а не время. Время задают три
     * других параметра (все — наша конфигурация выше):
     *
     *   max.block.ms       = 5 с  — период ДО создания future: ожидание
     *                               метаданных, когда bootstrap недоступен
     *                               (брокер лежит целиком)
     *   request.timeout.ms = 5 с  — ожидание ack на одну попытку
     *   delivery.timeout.ms= 10 с — вся жизнь send() целиком: все повторы
     *                               + ожидание ack
     *
     * Итоговое время отказа зависит от характера падения:
     *
     *  - Брокер лежит целиком (bootstrap недоступен): send() упирается в
     *    max.block.ms -> ~5 с и исключение. Это основной сценарий
     *    «Kafka недоступна», и поллер именно поэтому не висит.
     *  - Соединение рвётся быстро (connection refused): 3 попытки уходят
     *    почти мгновенно (между ними дефолтный retry.backoff.ms = 100 мс)
     *    -> отказ за доли секунды. Вот тут retries=3 против дефолтного
     *    Integer.MAX_VALUE даёт разницу: с дефолтом поллер долбил бы до
     *    истечения delivery.timeout (10 с), с 3 — сдаётся почти сразу и
     *    идёт дальше по пачке.
     *  - Запрос ушёл, ack не придёт: упирается в delivery.timeout -> ≤10 с.
     *
     * Плюс снаружи страховка: future.get(15 с) в OutboxDispatcher — но она
     * срабатывает редко, так как каждый send и так ограничен 10 с.
     *
     * Итого один «мёртвый» тик поллера занимает обычно ~5 с (метаданные),
     * а не 35 и не 120.
     *
     * ВАЖНО: фактическое число попыток ДОСТАВКИ в Kafka не ограничено
     * тройкой — оно ограничено OUTBOX-поллером: send() упал, строка
     * outbox_events осталась с sent_at IS NULL, следующий тик (200 мс)
     * шлёт её заново, и так бесконечно. retries=3 — только «быстрый
     * отказ внутри одного вызова», а не граница надёжности.
     *
     *
     * ---------------------------------------------------------------------
     * 2. CIRCUIT BREAKER: НЕ «РАНЬШЕ», А ПАРАЛЛЕЛЬНО И ВЗАИМНО
     * ---------------------------------------------------------------------
     *
     * FixedBackOff(1 с, 35) ретраит обработку уже ПРОЧИТАННОГО из топика
     * сообщения (вызов Payment + запись в БД). Circuit Breaker в
     * PaymentClient (Resilience4j: окно 6 вызовов, ≥50% таймаутов ->
     * OPEN, waitDurationInOpenState = 25 с = PAYMENT_BREAKER_WAIT_S,
     * в HALF_OPEN ровно 1 пробный вызов) решает, ДОСТИГНЕТ ЛИ попытка
     * сети. Хронология S4 (Payment ушёл в чёрную дыру, потом поднялся):
     *
     *   t=0     Payment падает. Consumer вызывает charge -> таймаут.
     *           Breaker считает неудачи -> после порога OPEN.
     *   t≈1с+   Ретрай #2 (FixedBackOff 1 с). Breaker OPEN -> вызов НЕ
     *           идёт в сеть, мгновенный отказ. Ретрай «бесплатен» по
     *           бюджету запросов S4 (≤30 за окно отказа) — в этом смысл
     *           OPEN-состояния.
     *   t=25с   Breaker переходит в HALF_OPEN. ОДИН пробный запрос в
     *           сеть (это сам очередной ретрай, см. ниже). Payment уже
     *           поднялся -> пробник успешен -> CLOSED.
     *   t≤26с   Ретраи #26..#35 реально звонят в Payment -> 201 -> заказ
     *           PAID. Оплачен КОНЬЮМЕР, не Reaper.
     *   t=35с   Бюджет ретраев исчерпан. Если оплата не случилась —
     *           событие пропускается (offset коммитится), заказ PENDING,
     *           подбирает Reaper (порог 30 с).
     *
     * То есть 35 попыток × 1 с ≈ 35 с — окно, которое должно ПЕРЕКРЫТЬ
     * 25-секундную паузу breaker'а. Цепочка «открылся -> ждёт 25 с ->
     * пробник -> закрылся» происходит РАНЬШЕ исчерпания ретраев, и это
     * расчёт, а не случайность: после закрытия breaker'а у consumer'а
     * ещё остаются попытки (26–35), и заказ доходит до paid своим ходом.
     *
     * Если бы ретраев было, скажем, 10 (10 с): бюджет кончился бы ДО
     * пробника на 25-й секунде -> событие отброшено, заказ лежал бы
     * PENDING до Reaper (порог 30 с) — это тоже не потеря, но оплата
     * позже и с нагрузкой на Reaper.
     *
     * Три уточнения к интуитивной модели «ретраи + предохранитель»:
     *
     *  a) Пробник — не отдельная фоновая процедура, таймера нет. Breaker
     *     ничего не делает сам: когда истекли 25 с, состояние меняется на
     *     HALF_OPEN, и САМЫЙ СЛЕДУЮЩИЙ вызов charge() (очередной ретрай
     *     коньюмера) и становится пробником. Без вызовов пробника не было
     *     бы вовсе. Пробник — это один из 35 ретраев, просто попавший в
     *     открытое окно.
     *  b) В OPEN ретрай не «ходит в сеть и получает отказ» — он вообще не
     *     выходит из JVM: charge() мгновенно возвращает NETWORK без
     *     единого пакета. Поэтому ретраи каждую секунду во время чёрной
     *     дыры не стоят ничего по бюджету запросов S4.
     *  c) CLOSED не одноразовый: после успеха пробника все вызовы идут в
     *     сеть снова, но если таймауты возобновятся (≥50% в окне последних
     *     6) — цепь снова уйдёт в OPEN. Это цикл, а не переключатель.
     *
     * Арифметика неудачного пробника: в бюджет ретраев обычно влезает
     * ровно ОДИН пробник (~26-я секунда). Провалился (Payment ещё лежит)
     * -> снова OPEN на 25 с, следующий пробник вышел бы на ~50-й, а
     * ретраи кончаются на 35-й. Событие пропускается, заказ PENDING.
     * Дальше включается Reaper: опрашивает каждые 10 с БЕЗ ограничения
     * попыток, сам ходит через тот же breaker (пока OPEN — мгновенный
     * fast-fail, бесплатно) и после первого успешного пробника оплачивает
     * заказ. Потеря невозможна в обоих случаях — просто платильщик разный.
     *
     *
     * ---------------------------------------------------------------------
     * 3. ЦЕНА ПАУЗЫ 1 с ПРОТИВ 5 с (почему FixedBackOff именно такой)
     * ---------------------------------------------------------------------
     *
     * Пока запись ретраится, seek-to-current возвращает её ПЕРВОЙ в
     * партицию — все события за ней в той же партиции ждут. Пауза между
     * попытками = простой всей партиции. С 5 с один «плохой» заказ держал
     * свою партицию ~30 с и соседние события не успевали дойти до Payment
     * за окно сценария (S2: 2 заказа с нулём запросов). С 1 с блокировка
     * партиции на попытку минимальна.
     *
     *
     * ИТОГО: retries=3 — про секунды/миллисекунды СЕТЕВОГО уровня,
     * FixedBackOff(1 с, 35) — про секунды БИЗНЕС-уровня, и последний
     * осознанно согласован с 25-секундным breaker'ом. Настоящие границы
     * надёжности — не эти числа, а OUTBOX (бесконечный повтор доставки)
     * и REAPER (бесконечный повтор оплаты зависшего заказа).
     * =====================================================================
     */
}
