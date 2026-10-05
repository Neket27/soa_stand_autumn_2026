package orders.config;

import orders.flow.BackgroundWorker;
import orders.flow.OrderProcessor;
import orders.flow.PaymentCircuitBreaker;
import orders.inventory.InventoryClient;
import orders.payment.PaymentClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableScheduling;

@Configuration
@EnableScheduling
public class AppConfig {

    @Value("${PAYMENT_URL:http://localhost:8080}")
    private String paymentUrl;

    @Value("${INVENTORY_URL:http://localhost:8080}")
    private String inventoryUrl;

    @Value("${PAYMENT.CONNECT_TIMEOUT_MS:200}")
    private int paymentConnectTimeoutMs;

    @Value("${PAYMENT.READ_TIMEOUT_MS:700}")
    private int paymentReadTimeoutMs;

    @Value("${INVENTORY.TIMEOUT_MS:200}")
    private int inventoryTimeoutMs;

    @Value("${CIRCUIT_BREAKER.OPEN_FOR_MS:30000}")
    private long circuitBreakerOpenForMs;

    @Bean
    public PaymentClient paymentClient() {
        return new PaymentClient(paymentUrl, paymentConnectTimeoutMs, paymentReadTimeoutMs);
    }

    @Bean
    public InventoryClient inventoryClient() {
        return new InventoryClient(inventoryUrl, inventoryTimeoutMs);
    }

    @Bean
    public PaymentCircuitBreaker paymentCircuitBreaker() {
        return new PaymentCircuitBreaker(circuitBreakerOpenForMs);
    }

    @Bean
    public OrderProcessor orderProcessor(PaymentClient paymentClient,
                                         InventoryClient inventoryClient,
                                         orders.store.OrderRepository orderRepository,
                                         PaymentCircuitBreaker circuitBreaker) {
        return new OrderProcessor(inventoryClient, paymentClient, orderRepository, circuitBreaker);
    }

    @Bean
    public BackgroundWorker backgroundWorker(orders.store.OrderRepository orderRepository,
                                              OrderProcessor orderProcessor,
                                              PaymentCircuitBreaker circuitBreaker) {
        return new BackgroundWorker(orderRepository, orderProcessor, circuitBreaker);
    }
}
