package orders.flow;

public enum OrderStatus {
    PENDING("pending"),
    PAID("paid"),
    REJECTED("rejected");

    private final String value;

    OrderStatus(String value) {
        this.value = value;
    }

    /** Lowercase form used in the DB and the HTTP API. */
    public String value() {
        return value;
    }

    public static OrderStatus fromValue(String value) {
        for (OrderStatus s : values()) {
            if (s.value.equals(value)) {
                return s;
            }
        }
        throw new IllegalArgumentException("unknown order status: " + value);
    }
}
