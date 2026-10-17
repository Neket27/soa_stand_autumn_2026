package orders.payment;

import orders.api.dto.PaymentChargeDto;
import lombok.extern.slf4j.Slf4j;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.client.ResourceAccessException;
import orders.store.OrderRepository;

import java.net.SocketTimeoutException;
import java.time.Duration;
import java.util.concurrent.TimeUnit;

/**
 * HTTP-клиент для Payment API.
 *
 * === КАК РАБОТАЕТ ===
 * POST {PAYMENT_URL}/payments с телом:
 * {
 *   "order_id": "...",
 *   "amount_cents": 1000,
 *   "currency": "RUB"
 * }
 * и заголовком Idempotency-Key: pay-{orderId}
 *
 * === КОДЫ ОТВЕТА ===
 * 201/200 — платёж успешен (CHARGED)
 * 402     — банк отклонил (DECLINED) — не ошибка, просто отказ
 * 500     — внутренняя ошибка Payment (SERVER_ERROR) — нужно retry
 * Сетевая ошибка / таймаут — Payment недоступен (NETWORK) — нужно retry
 *
 * === ПОВТОРЫ И ИДЕМПОТЕНТНОСТЬ (уже реализовано) ===
 *
 * 402 ловится отдельно через HttpStatusCodeException: RestTemplate
 * БРОСКАЕТ исключение на 4xx, и без отдельной ветки 402 превратился бы
 * в NETWORK, а заказ завис бы в PENDING.
 *
 * Повтор charge() с тем же Idempotency-Key возвращает Payment
 * сохранённый результат без нового списания — на это опираются retry-и
 * Consumer'а (500/сеть) и Reaper.
 *
 * === CIRCUIT BREAKER (защита от молчания Payment) ===
 *
 * Если Payment перестаёт отвечать (таймауты), вызовы из 12 потоков
 * коньюмера и из Reaper'ов долбили бы его бесконечно. Цепь Resilience4j
 * после порога таймаутов переходит в OPEN: запросы в сеть не
 * отправляются, charge() мгновенно возвращает NETWORK. Через
 * PAYMENT_BREAKER_WAIT_S — HALF_OPEN: один пробный запрос, успех
 * возвращает цепь в CLOSED. Отказом считаются ТОЛЬКО таймауты:
 * 500 и оборванные коннекты («потерянный ответ») — признаки живого
 * сервиса и в счётчик цепи не идут.
 */
@Slf4j
@Component
public class PaymentClient {

    private final RestTemplate restTemplate;
    private final OrderRepository orderRepository;

    /**
     * Circuit Breaker вызовов Payment (паттерн «предохранитель»).
     *
     * CLOSED  — штатный режим: вызовы идут в сеть, таймауты считаются
     *           отказами; доля отказов ≥ порога → OPEN.
     * OPEN    — сервис молчит: сетевые запросы НЕ отправляются вообще,
     *           charge() сразу возвращает NETWORK (fast-fail). Это
     *           разгружает и коньюмеры, и Reaper, и сам Payment.
     * HALF_OPEN — через waitDuration цепь пускает ровно ОДИН пробный
     *           запрос: успех → CLOSED, отказ → снова OPEN.
     */
    private final CircuitBreaker paymentBreaker;

    @Value("${PAYMENT_URL}")
    private String paymentUrl;

    public PaymentClient(OrderRepository orderRepository, RestTemplate restTemplate,
                         @Value("${PAYMENT_BREAKER_WAIT_S:25}") long breakerWaitSeconds) {
        this.orderRepository = orderRepository;
        // Настройка цепи под наш сценарий «сервис молчит» (S4):
        // - окно из последних 6 вызовов (COUNT_BASED), оцениваем только
        //   если их было не меньше 3 — иначе решение на пустом окне;
        // - 50% таймаутов в окне → OPEN;
        // - в HALF_OPEN — ровно 1 пробный вызов (канон паттерна):
        //   успех → CLOSED, отказ → снова OPEN;
        // - OPEN держится breakerWaitSeconds, затем проба.
        //
        // Пороги маленькие нарочно: во время чёрной дыры (S4 — 60 с,
        // бюджет ≤ 30 запросов) каждый поток со своей партицией и каждый
        // Reaper долбил бы Payment бесконечно. Цепь должна открываться
        // ПОСЛЕ ПЕРВОЙ же волны таймаутов (~3 вызова на JVM), а не
        // копить статистику. На честных сценариях (S1 402, S2 500,
        // S3 медленные, S5 потерянные ответы) таймаутов нет — цепь
        // никогда не открывается: отказом считаются только таймауты.
        CircuitBreakerConfig breakerConfig = CircuitBreakerConfig.custom()
                .slidingWindowType(CircuitBreakerConfig.SlidingWindowType.COUNT_BASED)
                .slidingWindowSize(6)
                .minimumNumberOfCalls(3)
                .failureRateThreshold(50f)
                .permittedNumberOfCallsInHalfOpenState(1)
                .waitDurationInOpenState(Duration.ofSeconds(breakerWaitSeconds))
                .build();
        this.paymentBreaker = CircuitBreaker.of("payment", breakerConfig);
        // Таймауты и пул соединений заданы в общем RestTemplate-бине
        // (orders.config.RestTemplateConfig): таймауты без RestTemplate'а
        // ждали бы ответ бесконечно, а при "зависшем" Payment навсегда
        // завис бы Consumer/Reaper; пул убирает connect+teardown на каждый
        // charge() (стресс-тест 1000/с иначе даёт хвост до 1.62 с).
        // Snake_case ObjectMapper подставляется там же.
        this.restTemplate = restTemplate;
    }

    /**
     * Попытка оплаты заказа (сумма читается из БД — путь Reaper'а).
     */
    public PaymentResult charge(String orderId) {
        return charge(orderId, orderRepository.getAmount(orderId));
    }

    /**
     * Попытка оплаты заказа с уже известной суммой.
     *
     * Kafka-Consumer уже читает статус+сумму одним запросом
     * (OrderRepository.getStatusAndAmount) — без этого параметра
     * charge() сделал бы ВТОРОЙ SELECT только ради amount_cents
     * (5000 лишних запросов/с на стресс-тесте 5000 заказов/с).
     */
    public PaymentResult charge(String orderId, long amountCents) {
        String idempotencyKey = "pay-" + orderId;

        // Ключ идемпотентности — в ЗАГОЛОВКЕ: так его читает заглушка
        // (r.Header.Get("Idempotency-Key")). Повторная отправка того же
        // заказа не создаёт второе списание.
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        headers.set("Idempotency-Key", idempotencyKey);

        PaymentChargeDto paymentChargeDto = new PaymentChargeDto(orderId, amountCents,"RUB");

        // Circuit Breaker: если цепь в OPEN (Payment молчит), запрос в
        // сеть НЕ отправляется — быстрый отказ без блокировки потока
        // на read timeout. Сюда попадают и коньюмеры, и Reaper.
        if (!paymentBreaker.tryAcquirePermission()) {
            return new PaymentResult(PaymentStatus.NETWORK,
                    "circuit breaker открыт, запрос не отправлен");
        }

        // Замер длительности — его ждёт onSuccess/onError (resilience4j 2.x).
        long startNanos = System.nanoTime();
        try {
            restTemplate.postForEntity(
                    paymentUrl + "/payments",
                    new HttpEntity<>(paymentChargeDto, headers),
                    String.class
            );
            // Только 2xx не бросают исключение
            paymentBreaker.onSuccess(System.nanoTime() - startNanos, TimeUnit.NANOSECONDS);
            return new PaymentResult(PaymentStatus.CHARGED, null);
        } catch (HttpStatusCodeException e) {
            // Ответ получен, но с ошибочным кодом. Для цепи это УСПЕХ:
            // сервис жив и отвечает, даже 500 — не «молчание». Счёт
            // отказов цепи ведётся только по таймаутам (ниже).
            paymentBreaker.onSuccess(System.nanoTime() - startNanos, TimeUnit.NANOSECONDS);
            // 402 → DECLINED (банк отклонил, резерв нужно снять)
            // 5xx и прочее → SERVER_ERROR (retry)
            int code = e.getStatusCode().value();
            if (code == 402)
                return new PaymentResult(PaymentStatus.DECLINED, "банк отклонил оплату");

            return new PaymentResult(PaymentStatus.SERVER_ERROR, "HTTP " + code);
        } catch (ResourceAccessException e) {
            // Сетевая ошибка: соединение не установлено или ТАЙМАУТ.
            if (hasCause(e, SocketTimeoutException.class)) {
                // Молчание (Payment принял соединение и не отвечает) —
                // признак, что сервис лёг: фиксируем отказ цепи. Накопит
                // порог — перейдёт в OPEN и перестанет слать запросы.
                paymentBreaker.onError(System.nanoTime() - startNanos, TimeUnit.NANOSECONDS, e);
                log.warn("Payment не отвечает (таймаут), state={} {}",
                        paymentBreaker.getState(), e.getMessage());
            } else {
                // Оборванный коннект при «потерянном ответе» (S5) — быстрый
                // обрыв, сервис при этом жив и обработал операцию. Как
                // отказ цепи НЕ считаем: сценарий S5 намеренно роняет
                // ответы на каждый первый запрос — иначе цепь открывалась
                // бы на честном сервисе.
                paymentBreaker.onSuccess(System.nanoTime() - startNanos, TimeUnit.NANOSECONDS);
            }
            return new PaymentResult(PaymentStatus.NETWORK, e.getMessage());
        } catch (Exception e) {
            // Прочие ошибки: Payment недоступен
            paymentBreaker.onSuccess(System.nanoTime() - startNanos, TimeUnit.NANOSECONDS);
            return new PaymentResult(PaymentStatus.NETWORK, e.getMessage());
        }
    }

    /** Ищем нужную причину в цепочке исключений RestTemplate. */
    private static boolean hasCause(Throwable e, Class<? extends Throwable> type) {
        for (Throwable t = e; t != null; t = t.getCause()) {
            if (type.isInstance(t)) {
                return true;
            }
        }
        return false;
    }

    public enum PaymentStatus {
        CHARGED,      // Оплачено
        DECLINED,     // Отклонено банком (402)
        SERVER_ERROR, // Ошибка сервера (500)
        NETWORK       // Сетевая ошибка / таймаут
    }

    public record PaymentResult(PaymentStatus status, String message) {}
}
