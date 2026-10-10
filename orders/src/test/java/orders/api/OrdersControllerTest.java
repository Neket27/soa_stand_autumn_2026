package orders.api;

import orders.api.dto.Order;
import orders.flow.CreateOrderService;
import orders.flow.OrderStatus;
import orders.store.OrderRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.validation.beanvalidation.LocalValidatorFactoryBean;

import java.math.BigDecimal;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@ExtendWith(MockitoExtension.class)
class OrdersControllerTest {

    @Mock
    private OrderRepository orderRepository;

    @Mock
    private CreateOrderService createOrderService;

    @Mock
    private OrderQueryService orderQueryService;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        OrdersController controller =
                new OrdersController(orderRepository, createOrderService, orderQueryService);
        LocalValidatorFactoryBean factory = new LocalValidatorFactoryBean();
        factory.afterPropertiesSet();
        mockMvc = MockMvcBuilders.standaloneSetup(controller)
                .setValidator(factory)
                .setControllerAdvice(new GlobalExceptionHandler())
                .build();
    }

    @Test
    void healthz_dbReady_returns200() throws Exception {
        when(orderRepository.isReady()).thenReturn(true);

        mockMvc.perform(get("/healthz"))
                .andExpect(status().isOk());
    }

    @Test
    void healthz_dbDown_returns503() throws Exception {
        when(orderRepository.isReady()).thenReturn(false);

        mockMvc.perform(get("/healthz"))
                .andExpect(status().isServiceUnavailable());
    }

    @Test
    void createOrder_validationFails_returns400() throws Exception {
        String body = """
                {"user_id":"","items":[{"sku":"sku-1","qty":1,"price_cents":100}]}
                """;

        mockMvc.perform(post("/orders")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isBadRequest());
    }

    @Test
    void createOrder_emptyItems_returns400() throws Exception {
        String body = """
                {"user_id":"u-1","items":[]}
                """;

        mockMvc.perform(post("/orders")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isBadRequest());
    }

    @Test
    void createOrder_pending_returns201Pending() throws Exception {
        String body = """
                {"user_id":"u-1","items":[{"sku":"sku-1","qty":2,"price_cents":49900}]}
                """;

        when(createOrderService.createOrder(any()))
                .thenReturn(new CreateOrderService.CreateResult("order-1", OrderStatus.PENDING));

        mockMvc.perform(post("/orders")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.id").value("order-1"))
                .andExpect(jsonPath("$.status").value("pending"));
    }

    @Test
    void createOrder_rejected_returns201Rejected() throws Exception {
        String body = """
                {"user_id":"u-1","items":[{"sku":"sku-1","qty":1,"price_cents":1000}]}
                """;

        when(createOrderService.createOrder(any()))
                .thenReturn(new CreateOrderService.CreateResult("order-3", OrderStatus.REJECTED));

        mockMvc.perform(post("/orders")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.id").value("order-3"))
                .andExpect(jsonPath("$.status").value("rejected"));
    }

    @Test
    void getOrder_found_returns200() throws Exception {
        when(orderQueryService.getOrder("order-1"))
                .thenReturn(new Order("order-1", "paid", new BigDecimal("99800")));

        mockMvc.perform(get("/orders/order-1"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.id").value("order-1"))
                .andExpect(jsonPath("$.status").value("paid"))
                .andExpect(jsonPath("$.amount_cents").value(99800));
    }

    @Test
    void getOrder_notFound_returns404WithErrorBody() throws Exception {
        when(orderQueryService.getOrder("nonexistent"))
                .thenThrow(new OrderNotFoundException("nonexistent"));

        mockMvc.perform(get("/orders/nonexistent"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.error").value("not_found"))
                .andExpect(jsonPath("$.message").value("order not found"));
    }
}
