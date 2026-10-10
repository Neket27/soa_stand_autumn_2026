package orders.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.math.BigDecimal;

public record Order(String id, String status,
                    @JsonProperty("amount_cents") BigDecimal amountCents) {
}
