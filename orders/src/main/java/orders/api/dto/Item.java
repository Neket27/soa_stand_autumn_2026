package orders.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;

public record Item(
        @NotBlank @JsonProperty("sku") String sku,
        @Min(1) int qty,
        @DecimalMin("0") @JsonProperty("price_cents") long priceCents) {
}
