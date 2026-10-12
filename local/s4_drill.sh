#!/usr/bin/env bash
# Dev-only S4-бюджет drill: blackhole Payment, N заказов во время отказа, замер
# payment.requests.in_blackhole (запросов к Payment за время отказа). Сравнивать:
#   BREAKER_STORE=db      -> глобальный бюджет (не умножается на N)
#   BREAKER_STORE=in-memory -> per-JVM, бюджет ~ умножается на N
set -uo pipefail
TOKEN="local-kafka-token"; B="http://127.0.0.1:8095"
R=(http://127.0.0.1:8081 http://127.0.0.1:8082 http://127.0.0.1:8083)
N=${1:-40}

curl -s -X POST -H "X-Admin-Token: $TOKEN" "$B/admin/reset" > /dev/null
curl -s -X PUT -H "X-Admin-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"payment":{"blackhole_s":20}}' "$B/admin/modes" > /dev/null
echo "blackhole 20s started $(date +%H:%M:%S)"
for i in $(seq 1 "$N"); do
  r=${R[$((i%3))]}
  curl -s -X POST "$r/orders" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"bh$i\",\"items\":[{\"sku\":\"sku-1\",\"qty\":1,\"price_cents\":1000}]}" > /dev/null
  [ $((i % 5)) -eq 0 ] && sleep 0.15
done
echo "sent $N orders during blackhole $(date +%H:%M:%S); waiting 30s..."
sleep 30
curl -s -H "X-Admin-Token: $TOKEN" "$B/admin/facts" | python3 -c "import sys,json; d=json.load(sys.stdin); p=d['payment']; r=p['requests']; print('in_blackhole =', r['in_blackhole'], '| by_status =', r['by_status'], '| charges =', p['charges']['total'])"
