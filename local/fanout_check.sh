#!/usr/bin/env bash
# Dev-only fan-out check: консьюмит event-topics (order.paid / order.rejected), считает
# total + distinct orderId. Invariant fan-out at-least-once: distinct == число terminal-
# заказов (каждый заказ получил >=1 событие), total >= distinct (дубли допустимы).
set -euo pipefail

KAFKA_CONTAINER="soa-local-kafka-kafka-1"
consume_topic() {
  docker exec "$KAFKA_CONTAINER" /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server localhost:9092 --topic "$1" --from-beginning \
    --max-messages 100000 --timeout-ms 6000 2>/dev/null
}

tmp_paid=$(mktemp); tmp_rej=$(mktemp)
consume_topic "order.paid"     > "$tmp_paid"
consume_topic "order.rejected" > "$tmp_rej"

total_paid=$(wc -l < "$tmp_paid")
distinct_paid=$(sed -n 's/.*"orderId":"\([^"]*\)".*/\1/p' "$tmp_paid" | sort -u | grep -c . || true)
total_rej=$(wc -l < "$tmp_rej")
distinct_rej=$(sed -n 's/.*"orderId":"\([^"]*\)".*/\1/p' "$tmp_rej" | sort -u | grep -c . || true)
rm -f "$tmp_paid" "$tmp_rej"

echo "== fan-out events (order.paid / order.rejected) =="
printf "  %-15s total=%-4s distinct_orders=%s\n" "order.paid" "$total_paid" "$distinct_paid"
printf "  %-15s total=%-4s distinct_orders=%s\n" "order.rejected" "$total_rej" "$distinct_rej"
echo "  total events:            $((total_paid + total_rej))"
echo "  distinct orders covered: $((distinct_paid + distinct_rej))"
