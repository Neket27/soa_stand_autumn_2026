/**
 * Общий HTTP-клиент с ПУЛИНГОМ соединений (Apache HttpClient 5).
 *
 * === ЗАЧЕМ (найдено стресс-тестом) ===
 *
 * Раньше оба клиента (Inventory, Payment) строили RestTemplate на
 * SimpleClientHttpRequestFactory — это JDK HttpURLConnection без
 * переиспользования соединений: КАЖДЫЙ вызов открывает новое
 * TCP-соединение и тут же закрывает его. На 1000 заказов/с это
 * ~200 connect+teardown в секунду на реплику → churn сокетов,
 * накопление TIME_WAIT, палево дескрипторов. Наш стресс-тест на
 * 1000/с показал 284/12000 ответов дольше 1000 мс (макс 1.62 с)
 * при ОТСУТСТВИИ каких-либо ошибок Hikari/GC/Tomcat в логах — то
 * есть хвост давал именно сетевой слой, а не БД.
 *
 * Решение — каноничная best practice для высоких нагрузок: Apache
 * HttpClient с PoolingHttpClientConnectionManager. Тёплые
 * соединения переиспользуются (keep-alive), их число ограничено
 * пулом, ожидание соединения имеет собственный таймаут.
 *
 * === СОГЛАСОВАННОСТЬ С CIRCUIT BREAKER (S4) ===
 *
 * Смена фабрики меняет ТИПЫ исключений, а PaymentClient
 * классифицирует отказ по java.net.SocketTimeoutException в цепочке.
 * Apache HttpClient при read timeout бросает именно
 * SocketTimeoutException (его кидает JDK-сокет под капотом HC5),
 * поэтому детект «молчания Payment» не ломается — это проверяется
 * прогоном S4 после изменения. Read timeout держим прежним (10 с),
 * чтобы семантика не поплыла.
 */
package orders.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.hc.client5.http.config.ConnectionConfig;
import org.apache.hc.client5.http.config.RequestConfig;
import org.apache.hc.client5.http.impl.classic.CloseableHttpClient;
import org.apache.hc.client5.http.impl.classic.HttpClients;
import org.apache.hc.client5.http.impl.io.PoolingHttpClientConnectionManager;
import org.apache.hc.core5.util.TimeValue;
import org.apache.hc.core5.util.Timeout;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.HttpComponentsClientHttpRequestFactory;
import org.springframework.http.converter.HttpMessageConverter;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.web.client.RestTemplate;

@Configuration
public class RestTemplateConfig {

    @Bean
    public RestTemplate restTemplate(ObjectMapper objectMapper) {
        // --- Пул соединений ---
        // Ограничения на реплику. Оба хоста (Inventory, Payment) — один
        // сервис mocks, но у URL могут быть разные route. Считаем пул от
        // реальной конкурентности: 1000/с × 5 мс ≈ 5 одновременных
        // вызова + запас под бёрсты и удержание соединений во время
        // read timeout (S4, до 10 с на вызов, но цепь быстро открывается
        // и перестаёт держать соединения). 75 на route с огромным
        // запасом не расходуется; главное — НЕ исчерпывается (иначе
        // ConnectionRequestTimeoutException → 5xx).
        PoolingHttpClientConnectionManager connectionManager =
                new PoolingHttpClientConnectionManager();
        connectionManager.setMaxTotal(150);          // весь пул на реплику
        connectionManager.setDefaultMaxPerRoute(75); // на один хост (mocks)
        connectionManager.setDefaultConnectionConfig(ConnectionConfig.custom()
                // Connect: прежние 5 с (как было в SimpleClientHttpRequestFactory)
                .setConnectTimeout(Timeout.ofSeconds(5))
                // Read (socket) — прежние 10 с: именно он даёт
                // java.net.SocketTimeoutException для Circuit Breaker (S4)
                .setSocketTimeout(Timeout.ofSeconds(10))
                // Жизнь соединения: дольше живёт — больше переиспользования,
                // но не вечно, чтобы балансировать с TIME_WAIT/браком
                .setTimeToLive(TimeValue.ofMinutes(5))
                .build());

        // --- Таймауты на запрос ---
        RequestConfig requestConfig = RequestConfig.custom()
                // Ожидание свободного соединения из пула: отдельный таймаут,
                // чтобы при гипотетическом исчерпании пула запрос упал
                // предсказуемо, а не завис до бесконечности (бюджет 1 с)
                .setConnectionRequestTimeout(Timeout.ofSeconds(2))
                .build();

        CloseableHttpClient httpClient = HttpClients.custom()
                .setConnectionManager(connectionManager)
                .setDefaultRequestConfig(requestConfig)
                // Подчистка протухших/испорченных соединений из пула
                .evictExpiredConnections()
                .build();

        HttpComponentsClientHttpRequestFactory factory =
                new HttpComponentsClientHttpRequestFactory(httpClient);
        RestTemplate restTemplate = new RestTemplate(factory);

        // Подставляем контекстный ObjectMapper: только он знает snake_case-
        // стратегию (JacksonConfig). Без неё DTO ушёл бы в JSON как
        // {"orderId":...}, а сервисы ждут {"order_id":...}.
        for (HttpMessageConverter<?> converter : restTemplate.getMessageConverters()) {
            if (converter instanceof MappingJackson2HttpMessageConverter jackson) {
                jackson.setObjectMapper(objectMapper);
            }
        }
        return restTemplate;
    }
}
