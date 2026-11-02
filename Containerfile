# Immagine di sazed-agent: Ubuntu 26.04 con i soli pacchetti apt che servono.
# Dati, segreti e configurazione non stanno qui: arrivano montati dal computer.
FROM docker.io/library/ubuntu:26.04

# python3-telethon: client MTProto, per scaricare i file oltre i 20 MB dell'API dei bot.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-websockets python3-telethon \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY sazed_agent/ sazed_agent/
COPY tests/ tests/
COPY scripts/ scripts/
COPY config.example.toml ./

ENTRYPOINT ["python3", "-m", "sazed_agent"]
