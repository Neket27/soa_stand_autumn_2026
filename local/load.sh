#!/usr/bin/env bash
# Dev-only load harness: N заказов round-robin на 3 Kafka-реплики orders, затем ожидание
# всех terminal-состояний и отчёт. Запрос статуса уходит на любую реплику (общая БД).
set -euo pipefail

REPLICAS=("http://127.0.0.1:8081" "http://127.0.0.1:8082" "http://127.0.0.1:8083")
N=${1:-30}
STATUS_URL="http://127.0.0.1:8081"

json_id()  { sed -n 's/.*"id":"\([^"]*\)".*/\1/p'; }
json_st()  { sed -n 's/.*"status":"\([^"]*\)".*/\1/p'; }

echo "== healthz =="
for r in "${REPLICAS[@]}"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$r/healthz")
  echo "  $r -> $code"
done

echo "== отправляю $N заказов (round-robin) =="
ids=()
sent=0
failed=0
for ((i=0; i<N; i++)); do
  r=${REPLICAS[$((i % 3))]}
  resp=$(curl -s -X POST "$r/orders" -H 'Content-Type: application/json' \
    -d '{"user_id":"u'$i'","items":[{"sku":"sku-1","qty":1,"price_cents":1000}]}') || { failed=$((failed+1)); continue; }
  id=$(echo "$resp" | json_id)
  if [ -n "$id" ]; then ids+=("$id"); sent=$((sent+1)); else failed=$((failed+1)); fi
done
echo "  отправлено=$sent ошибок=$failed"

echo "== ожидание terminal-состояний (до 90 c) =="
for ((round=0; round<90; round++)); do
  pending=0
  for id in "${ids[@]}"; do
    st=$(curl -s "$STATUS_URL/orders/$id" | json_st)
    [ "$st" = "pending" ] && pending=$((pending+1))
  done
  if [ "$pending" -eq 0 ]; then echo "  всё terminal за ${round} c"; break; fi
  sleep 1
done

paid=0; rejected=0; pending=0
for id in "${ids[@]}"; do
  st=$(curl -s "$STATUS_URL/orders/$id" | json_st)
  case "$st" in
    paid) paid=$((paid+1)) ;;
    rejected) rejected=$((rejected+1)) ;;
    *) pending=$((pending+1)) ;;
  esac
done
echo "== итог: paid=$paid rejected=$rejected pending=$pending (из $sent) =="
if [ "$pending" -gt 0 ]; then
  echo "FAIL: есть pending"; exit 1
fi
echo "PASS: все заказы завершены"
