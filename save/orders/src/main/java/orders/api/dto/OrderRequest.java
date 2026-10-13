package orders.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;

import java.math.BigDecimal;
import java.util.List;

public record OrderRequest(
        @NotBlank @JsonProperty("user_id") String userId,
        @NotEmpty @JsonProperty("items") List<Item> items) {

    /** Правило суммы заказа: сумма qty * price_cents по всем позициям, в центах. */
    public BigDecimal amountCents() {
        return items.stream()
                .reduce(BigDecimal.ZERO, (sum, i) ->
                        sum.add(BigDecimal.valueOf(i.qty()).multiply(i.priceCents())),
                        BigDecimal::add);
    }
}
