# Imagem para rodar o watchdog fora do Fabric — o cenário recomendado, já que assim o
# watchdog não consome CU da capacidade que ele mede.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir pbi-watchdog

# A config não traz segredos: eles vêm por variável de ambiente / secret store.
COPY watchdog.yaml /app/watchdog.yaml

# ATENÇÃO: /data precisa ser um volume persistente. É onde vive a baseline.
VOLUME ["/data"]
ENV WATCHDOG_DB=/data/watchdog.db

# Um ciclo por invocação; o agendamento fica com o orquestrador (cron do host, K8s
# CronJob, Container Apps job). Use `--loop` só se o container for de vida longa.
ENTRYPOINT ["pbi-watchdog", "--config", "/app/watchdog.yaml"]
CMD ["run"]
