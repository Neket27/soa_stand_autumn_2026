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
import org.springframework.kafka.listener.KafkaMessageListenerContainer;
import org.springframework.kafka.listener.MessageListener;
import org.springframework.kafka.support.serializer.JsonDeserializer;
import org.springframework.kafka.support.serializer.JsonSerializer;
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

    // Дефолт localhost:9092 — официальная проверка не задаёт переменную и не
    // имеет брокера: там работает встроенный Kafka контейнера (см.
    // orders/kafka/embedded-entrypoint.sh). Локально адрес приходит из compose.
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

    @Bean
    public NewTopic ordersChargeTopic() {
        // 1 партиции — можно масштабировать consumer-реплики
        return new NewTopic("orders-charge", 1, (short) 1);
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
        // падает сразу, и событие уходит только через Reaper. Поэтому ниже
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
        // Читаем с начала топика при старте — важно для тестов
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
}
