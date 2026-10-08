package orders.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import orders.flow.BackgroundWorker;
import orders.flow.OrderProcessor;
import orders.store.OrderRepository;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.validation.beanvalidation.LocalValidatorFactoryBean;

import java.util.List;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@ExtendWith(MockitoExtension.class)
class OrdersControllerTest {

    private MockMvc mockMvc;

    @Mock
    private OrderRepository orderRepository;

    @Mock
    private OrderProcessor orderProcessor;

    @Mock
    private BackgroundWorker backgroundWorker;

    private final ObjectMapper objectMapper = new ObjectMapper();
    private OrdersController controller;

    @org.junit.jupiter.api.BeforeEach
    void setUp() {
        controller = new OrdersController(orderRepository, orderProcessor, backgroundWorker, objectMapper, 900);
        LocalValidatorFactoryBean factory = new LocalValidatorFactoryBean();
        factory.afterPropertiesSet();
        mockMvc = MockMvcBuilders.standaloneSetup(controller)
                .setValidator(factory)
                .build();
    }

    @Test
    void healthz_returns200() throws Exception {
        mockMvc.perform(get("/healthz"))
                .andExpect(status().is2xxSuccessful());
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
    void createOrder_incomplete_returns201Pending() throws Exception {
        String body = """
                {"user_id":"u-1","items":[{"sku":"sku-1","qty":2,"price_cents":49900}]}
                """;

        when(orderRepository.insertPending(anyString(), anyList(), anyLong())).thenReturn("order-1");
        when(orderProcessor.processOnce(anyString(), eq(99800L), anyString(), any())).thenReturn("PENDING");

        mockMvc.perform(post("/orders")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.id").value("order-1"))
                .andExpect(jsonPath("$.status").value("pending"));

        verify(backgroundWorker).enqueue("order-1");
    }

    @Test
    void createOrder_rejected_returns201Rejected() throws Exception {
        String body = """
                {"user_id":"u-1","items":[{"sku":"sku-1","qty":1,"price_cents":1000}]}
                """;

        when(orderRepository.insertPending(anyString(), anyList(), anyLong())).thenReturn("order-3");
        when(orderProcessor.processOnce(anyString(), eq(1000L), anyString(), any())).thenReturn("REJECTED");
        when(orderRepository.transition(anyString(), eq("rejected"))).thenReturn(1);

        mockMvc.perform(post("/orders")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.id").value("order-3"))
                .andExpect(jsonPath("$.status").value("rejected"));
    }

    @Test
    void getOrder_found_returns200() throws Exception {
        OrderRepository.Order order = new OrderRepository.Order("order-1", "u-1", "paid", 99800, "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);

        mockMvc.perform(get("/orders/order-1"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.id").value("order-1"))
                .andExpect(jsonPath("$.status").value("paid"))
                .andExpect(jsonPath("$.amount_cents").value(99800));
    }

    @Test
    void getOrder_notFound_returns404() throws Exception {
        when(orderRepository.findById("nonexistent")).thenReturn(null);

        mockMvc.perform(get("/orders/nonexistent"))
                .andExpect(status().isNotFound());
    }

    @Test
    void amountCents_calculatedCorrectly() throws Exception {
        String body = """
                {"user_id":"u-1","items":[{"sku":"a","qty":2,"price_cents":49900},{"sku":"b","qty":1,"price_cents":1000}]}
                """;

        when(orderRepository.insertPending(anyString(), anyList(), eq(100800L))).thenReturn("order-4");
        when(orderProcessor.processOnce(anyString(), eq(100800L), anyString(), any())).thenReturn("INCOMPLETE");

        mockMvc.perform(post("/orders")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isCreated());
    }
}
