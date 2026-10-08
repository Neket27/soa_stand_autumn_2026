package orders.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;

import java.util.List;

public record OrderRequest(
        @NotBlank @JsonProperty("user_id") String userId,
        @NotEmpty @JsonProperty("items") List<Item> items) {

    /** Order amount rule: sum of qty * price_cents over all items. */
    public long amountCents() {
        return items.stream()
                .mapToLong(i -> (long) i.qty() * i.priceCents())
                .sum();
    }
}
