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
import org.springframework.kafka.listener.ContainerProperties;
import org.springframework.kafka.listener.KafkaMessageListenerContainer;
import org.springframework.kafka.listener.MessageListener;
import org.springframework.kafka.support.serializer.JsonDeserializer;
import org.springframework.kafka.support.serializer.JsonSerializer;

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
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 * S1: Payment отклоняет 10% платежей (402). Нужно убедиться, что:
 *     — Consumer корректно обработал 402 → REJECTED
 *     — DELETE /reservations/{orderId} вызван для снятия резерва
 *
 * S2: Payment возвращает 500 на 33% запросов. Нужно убедиться, что:
 *     — Consumer retry-ит запрос при 500
 *     — После успешного retry заказ переходит в PAID
 *     — Минимум 90% заказов (не отклонённых банком) становятся PAID
 */
@Configuration
@EnableKafka
public class KafkaConfig {

    @Value("${KAFKA_BOOTSTRAP_SERVERS}")
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
        // 3 партиции — можно масштабировать consumer-реплики
        return new NewTopic("orders-charge", 3, (short) 1);
    }

    /**
     * Producer — отправляет события о заказах в Kafka.
     * Используется, когда ORDERS_DISPATCH=kafka (как в docker-compose.local.yml).
     */
    @Bean
    public ProducerFactory<String, String> producerFactory() {
        Map<String, Object> props = new HashMap<>();
        props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        props.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        // Важно: ждём подтверждения от всех реплик
        props.put(ProducerConfig.ACKS_CONFIG, "all");
        // Автоматический retry при сбое сети
        props.put(ProducerConfig.RETRIES_CONFIG, 3);
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
        // Коммитим offset вручную черAckMode.MANUAL
        props.put(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, false);
        return new DefaultKafkaConsumerFactory<>(props);
    }
}
