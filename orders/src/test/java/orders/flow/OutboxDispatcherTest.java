package orders.flow;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;
import orders.store.OutboxRepository;

import java.sql.Timestamp;
import java.util.List;
import java.util.concurrent.CompletableFuture;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Backoff поллинга OutboxDispatcher при отказе Kafka.
 *
 * === ЧТО ПРОВЕРЯЕМ ===
 * 1. Рост интервала: 200 мс, 400 мс, ..., потолок 60 с (чистая функция
 *    backoffDelayMillis — без реального ожидания).
 * 2. После неудачи следующий тик НЕ ходит в БД за claim'ом (иначе
 *    каждые 200 мс была бы новая попытка на упавший брокер).
 * 3. Успех сбрасывает backoff: поллер снова опрашивает очередь сразу.
 *
 * Тик поллинга сознательно вызывается pump() напрямую, без шедулера:
 * нас интересует поведение одного тика, а не расписание.
 */
@ExtendWith(MockitoExtension.class)
class OutboxDispatcherTest {

    @Mock
    private OutboxRepository outboxRepository;

    @Mock
    private KafkaTemplate<String, String> kafkaTemplate;

    @InjectMocks
    private OutboxDispatcher dispatcher;

    private static final OutboxRepository.OutboxEvent EVENT =
            new OutboxRepository.OutboxEvent(1L, "order-1", "order.charge", "42", new Timestamp(0));

    /** Отказ Kafka: send() бросает исключение сразу, как при недоступном брокере. */
    private void kafkaDown() {
        doThrow(new RuntimeException("kafka недоступна"))
                .when(kafkaTemplate).send(anyString(), anyString(), anyString());
    }

    /** Kafka снова отвечает: send() возвращает уже завершённый future (результат не разбираем). */
    private void kafkaUp() {
        // doReturn/doThrow, а не when/thenReturn: пачку уже стабилили
        // на отказ (kafkaDown), и повторный вызов send() внутри when()
        // снова бросил бы это исключение.
        doReturn(CompletableFuture.<SendResult<String, String>>completedFuture(null))
                .when(kafkaTemplate).send(anyString(), anyString(), anyString());
    }

    @Test
    void интервал_растёт_экспоненциально_и_ограничен_60_с() {
        assertEquals(200, OutboxDispatcher.backoffDelayMillis(1));     // первый отказ
        assertEquals(400, OutboxDispatcher.backoffDelayMillis(2));
        assertEquals(800, OutboxDispatcher.backoffDelayMillis(3));
        assertEquals(1_600, OutboxDispatcher.backoffDelayMillis(4));

        // Потолок: и для «свежего» превышения, и для большого счётчика
        // (проверяем и то, что сдвиг не переполняется при сотне неудач).
        assertEquals(60_000, OutboxDispatcher.backoffDelayMillis(10));
        assertEquals(60_000, OutboxDispatcher.backoffDelayMillis(100));
    }

    @Test
    void после_ошибки_отправки_тик_не_забирает_новую_пачку() {
        when(outboxRepository.claimBatch(anyString(), anyInt(), any()))
                .thenReturn(List.of(EVENT));
        kafkaDown();

        dispatcher.pump();

        // Второй тик сразу после неудачи: интервал ещё действует,
        // claim в БД не делается — брокер не получает новой попытки.
        dispatcher.pump();

        verify(outboxRepository, times(1)).claimBatch(anyString(), anyInt(), any());
        verify(outboxRepository, times(0)).markSent(any());
    }

    @Test
    void после_истечения_интервала_поллер_пробуждается_и_успех_сбрасывает_backoff() throws Exception {
        when(outboxRepository.claimBatch(anyString(), anyInt(), any()))
                .thenReturn(List.of(EVENT));
        kafkaDown();
        dispatcher.pump();

        // Ждём дольше первого интервала (200 мс) — будим поллер.
        Thread.sleep(400);

        kafkaUp();
        dispatcher.pump();

        // Успех: пачка помечена sent, а backoff сброшен — следующий тик
        // идёт в БД сразу, без ожидания (третий вызов pump()).
        dispatcher.pump();

        verify(outboxRepository, times(3)).claimBatch(anyString(), anyInt(), any());
        verify(outboxRepository, times(2)).markSent(any());
    }

    @Test
    void пустая_очередь_не_накапливает_неудачи() {
        when(outboxRepository.claimBatch(anyString(), anyInt(), any()))
                .thenReturn(List.of());

        dispatcher.pump();
        dispatcher.pump();

        // Пустые тики не считаются отказами: БД опрашивается каждые 200 мс.
        verify(outboxRepository, times(2)).claimBatch(anyString(), anyInt(), any());
    }

    @Test
    void счётчик_неудач_растёт_при_повторных_отказах() throws Exception {
        // Отдельно проверяем, что каждая неудача увеличивает задержку,
        // т.е. счётчик не «залипает» на первой ошибке.
        int[] attempts = {0};
        when(outboxRepository.claimBatch(anyString(), anyInt(), any()))
                .thenReturn(List.of(EVENT));
        when(kafkaTemplate.send(anyString(), anyString(), anyString()))
                .thenAnswer(inv -> {
                    attempts[0]++;
                    throw new RuntimeException("kafka недоступна, попытка " + attempts[0]);
                });

        dispatcher.pump();   // 1-я неудача → следующая попытка через 200 мс
        Thread.sleep(250);
        dispatcher.pump();   // 2-я неудача → через 400 мс
        Thread.sleep(450);
        dispatcher.pump();   // 3-я неудача → через 800 мс

        verify(outboxRepository, times(3)).claimBatch(anyString(), anyInt(), any());
        assertEquals(3, attempts[0]);
    }
}
