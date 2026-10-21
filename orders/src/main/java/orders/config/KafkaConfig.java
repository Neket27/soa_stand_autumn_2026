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
 * После резерва заказ и событие оплаты атомарно сохраняются в БД. Затем
 * OutboxDispatcher публикует в Kafka id заказа; consumer читает этот id
 * и вызывает Payment API.
 *
 * === КАК ЭТО РАБОТАЕТ ===
 * 1. Producer отправляет id заказа в топик "orders-charge"
 * 2. Consumer читает событие и вызывает Payment.charge()
 * 3. При успехе — заказ переходит в PAID
 * 4. При отказе (402) — заказ переходит в REJECTED, резерв снимается
 * 5. При ошибке (500/сети) DefaultErrorHandler повторно вызывает обработчик
 *    той же записи; повторной публикации записи в топик нет.
 *
 * === ПОЧЕМУ KAFKA, А НЕ ПРЯМОЙ ВЫЗОВ ===
 * — Асинхронность: POST /orders не ждёт ответа Payment.
 * — Разделение этапов: outbox доставляет id в Kafka, consumer вызывает Payment.
 * — Повторы обработки ограничены; зависший PENDING-заказ может подобрать Reaper.
 *
 * === КОНТРАКТ С ЗАГЛУШКАМИ (что проверяет раннер) ===
 * S1: Payment отклоняет 10% платежей (402) → заказ REJECTED, резерв снят
 * S2: Payment возвращает 500 на 33% запросов → consumer повторно
 *     обрабатывает ту же запись; после успешной обработки заказ переходит в PAID
 */
@Configuration
@EnableKafka
public class KafkaConfig {

    @Value("${KAFKA_BOOTSTRAP_SERVERS:localhost:9092}")
    private String bootstrapServers;

    @Value("${KAFKA_GROUP_ID:orders-charge}")
    private String groupId;

    /**
     * Бин AdminClient с адресом брокера. Сам этот бин в приложении
     * не вызывает createTopics; параметры топика объявлены NewTopic-бином.
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
        // Ждём подтверждения от всех ISR (у объявленного ниже топика RF=1).
        props.put(ProducerConfig.ACKS_CONFIG, "all");

        // Максимальное число внутренних повторов producer при восстанавливаемых ошибках.
        // Это отдельный уровень от повторной обработки consumer'ом ниже.
        // Если отправка завершится ошибкой, OutboxDispatcher не пометит
        // пачку sent; её claim останется до истечения visibility timeout
        // (60 с), после чего строки снова можно будет забрать из outbox.
        // Общие временные пределы send задаются параметрами ниже.
        props.put(ProducerConfig.RETRIES_CONFIG, 3);

        // delivery.timeout.ms — верхняя граница жизни send() целиком
        // (все повторы + ожидание ack). Явно фиксируем, чтобы поведение
        // не зависело от дефолтов версии клиента.
        //
        // ЛОВУШКА: у Kafka строгое правило валидации
        //   delivery.timeout.ms >= linger.ms + request.timeout.ms
        // Поэтому вместе с delivery.timeout.ms ниже явно задаём
        // request.timeout.ms = 5000.
        props.put(ProducerConfig.REQUEST_TIMEOUT_MS_CONFIG, 5_000);
        props.put(ProducerConfig.DELIVERY_TIMEOUT_MS_CONFIG, 10_000);

        // max.block.ms — вторая, «верхняя» граница send(). Ограничивает
        // период ДО создания future: ожидание метаданных (брокеры лежат,
        // bootstrap недоступен) и ожидание освобождения буфера
        // (buffer.memory). delivery.timeout.ms сюда НЕ входит — будущего
        // (future) в этот момент ещё не существует.
        //
        // Дефолт 60 с: отдельный вызов send() мог бы до минуты ждать
        // метаданные/буфер при недоступной Kafka. Ограничиваем такое
        // ожидание 5 с; это предел одного send(), не всего тика поллера.
        props.put(ProducerConfig.MAX_BLOCK_MS_CONFIG, 5_000);
        return new DefaultKafkaProducerFactory<>(props);
    }

    @Bean
    public KafkaTemplate<String, String> kafkaTemplate() {
        return new KafkaTemplate<>(producerFactory());
    }

    /**
     * Consumer — читает события из топика и вызывает Payment API.
     * ContainerProperties.AckMode.MANUAL — listener подтверждает
     * обработанные записи вручную; неуспешные передаются error handler'у,
     * который повторяет обработку и выполняет recovery после исчерпания
     * повторов.
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
     * Здесь настроен повтор обработки неудачных событий. Стандартный
     * DefaultErrorHandler без явной настройки использовал бы
     * FixedBackOff(0, 9): до 9 повторов без паузы.
     *
     * FixedBackOff(1 с, 35 повторов): после первой неудачной обработки
     * повторно вызывается обработчик той же записи. Это не публикация
     * новой записи в топик: seek-to-current оставляет исходную запись
     * следующей для обработки в её партиции, задерживая записи за ней.
     * 35 — число повторов после первой попытки, а не общее число вызовов.
     *
     * Интервал — только пауза между обработками; полное время зависит также
     * от длительности каждого вызова Payment. Circuit Breaker с настройкой
     * по умолчанию 25 с может быстро отклонять вызовы в OPEN, но это не
     * гарантирует, что Payment восстановится до исчерпания повторов.
     * После исчерпания повторов DefaultErrorHandler запускает recovery по
     * умолчанию и не публикует запись заново. Если заказ всё ещё PENDING,
     * его может подобрать Reaper после порога в 30 с. Reaper работает
     * независимо, поэтому возможна гонка с последними обработками consumer'а.
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
     * 1. ГРАНИЦЫ ОДНОГО ВЫЗОВА PRODUCER
     * ---------------------------------------------------------------------
     *
     * retries=3 — настройка внутренних повторов producer при
     * восстанавливаемых ошибках, а не число повторных запусков outbox.
     * Временные границы одного send задаются также параметрами выше:
     *
     *   max.block.ms       = 5 с  — период ДО создания future: ожидание
     *                               метаданных, когда bootstrap недоступен
     *                               (брокер лежит целиком)
     *   request.timeout.ms = 5 с  — ожидание ack на одну попытку
     *   delivery.timeout.ms= 10 с — вся жизнь send() целиком: все повторы
     *                               + ожидание ack
     *
     * max.block.ms ограничивает ожидание метаданных/буфера до возврата
     * future; delivery.timeout.ms ограничивает жизнь отправки после этого.
     * Конкретное время ошибки зависит от состояния соединения и брокера,
     * поэтому retries=3 само по себе не означает фиксированную длительность.
     * OutboxDispatcher ждёт future.get(15 с) для каждого события отдельно;
     * это не общий дедлайн для всей пачки.
     *
     * При ошибке отправки outbox не помечает пачку sent. Её строки остаются
     * claimed и станут доступны повторно после visibility timeout 60 с;
     * следующий тик через 200 мс может взять другие, ещё не claimed строки.
     * Поэтому повторная доставка одной и той же пачки не происходит на
     * ближайшем тике.
     *
     *
     * ---------------------------------------------------------------------
     * 2. CIRCUIT BREAKER И ПОВТОРЫ CONSUMER'А
     * ---------------------------------------------------------------------
     *
     * FixedBackOff относится к повторной обработке записи, уже прочитанной
     * из топика; producer RETRIES_CONFIG относится только к send в Kafka.
     * Время consumer-цикла включает и интервалы backoff, и обработку
     * (в том числе HTTP timeout). Поэтому 35 повторов не равны гарантированным
     * 35 секундам. В OPEN Circuit Breaker charge() возвращает NETWORK без
     * сетевого вызова; после перехода в HALF_OPEN один из последующих вызовов
     * может стать пробным. Точное число попыток до восстановления зависит
     * от момента открытия breaker'а и результата пробного вызова.
     *
     * Ни producer retry, ни consumer retry не заменяют устойчивое хранение:
     * outbox повторно предлагает неотправленные события после истечения
     * claim lease, а Reaper периодически повторяет оплату PENDING-заказов.
     * =====================================================================
     */
}
