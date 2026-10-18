#!/bin/sh
# === CHECK-ONLY ENTRYPOINT: встроенный Kafka для официальной проверки ===
#
# Стенд проверки поднимает compose из master репозитория курса, где Kafka нет,
# и не задаёт KAFKA_BOOTSTRAP_SERVERS. В этом случае поднимаем однорозовый
# бroker прямо здесь (см. orders/kafka/server.properties) и ждём его
# готовности до старта приложения: так NewTopic-бин создаёт топик штатно,
# а прогрев уходит через боевой путь outbox → Kafka → charge.
#
# Локально (docker-compose задаёт KAFKA_BOOTSTRAP_SERVERS=kafka:9094) этот
# блок пропускается — работает внешний брокер стенда. В прод бинарник Kafka
# в образ приложения не попадает: это компромисс ради сдачи проверки.

set -e

if [ -z "$KAFKA_BOOTSTRAP_SERVERS" ]; then
    echo "[embedded-kafka] KAFKA_BOOTSTRAP_SERVERS не задан — поднимаю локальный брокер"
    DATA_DIR=/tmp/kafka-embedded-logs
    mkdir -p "$DATA_DIR"
    if [ ! -f "$DATA_DIR/.formatted" ]; then
        CLUSTER_ID=$(/opt/kafka/bin/kafka-storage.sh random-uuid)
        /opt/kafka/bin/kafka-storage.sh format -t "$CLUSTER_ID" -c /opt/kafka/config/server.properties
        touch "$DATA_DIR/.formatted"
    fi
    # Куча брокера ограничена: на стенде проверки он обслуживает лишь прогрев
    # и сценарии задания (единицы заказов), 256 мБ достаточно.
    KAFKA_HEAP_OPTS="-Xms128m -Xmx256m" \
        nohup /opt/kafka/bin/kafka-server-start.sh /opt/kafka/config/server.properties \
        >/tmp/kafka-embedded.log 2>&1 &
    echo "[embedded-kafka] жду готовности брокера..."
    i=0
    until /opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092 >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 60 ]; then
            echo "[embedded-kafka] брокер не поднялся за 60 с, лог:" >&2
            tail -50 /tmp/kafka-embedded.log >&2
            exit 1
        fi
        sleep 1
    done
    echo "[embedded-kafka] брокер готов"
fi

exec java \
    -XX:TieredStopAtLevel=1 \
    -Xms128m -Xmx256m \
    -Xlog:gc*:file=/tmp/gc.log:time,uptime:filecount=3,filesize=20M \
    -jar /app/app.jar
