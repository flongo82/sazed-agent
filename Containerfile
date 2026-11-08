# Immagine di sazed-agent: Ubuntu 26.04 con i soli pacchetti apt che servono.
# Dati, segreti e configurazione non stanno qui: arrivano montati dal computer.
FROM docker.io/library/ubuntu:26.04

# python3-telethon: client MTProto, per scaricare i file oltre i 20 MB dell'API dei bot.
# python3-anthropic: l'SDK ufficiale dell'API di Claude, per intenzione e classificazione.
# ffmpeg: l'audio dei vocali e dei video per la trascrizione, e qualche fotogramma dei video.
# whisper.cpp: la trascrizione, sul processore; il modello sta in models/, montato dal computer.
# yt-dlp: descrizione, sottotitoli e, se mancano, audio dei video di YouTube.
# pip serve solo a installare pip-requirements.txt (ciò che apt non ha), poi se ne va.
COPY pip-requirements.txt /tmp/
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-websockets python3-telethon \
      python3-anthropic ffmpeg whisper.cpp yt-dlp ca-certificates python3-pip \
 && pip install --no-deps --require-hashes --only-binary=:all: --no-cache-dir \
      --break-system-packages -r /tmp/pip-requirements.txt \
 && apt-get purge -y --auto-remove python3-pip \
 && rm -rf /var/lib/apt/lists/* /tmp/pip-requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY sazed_agent/ sazed_agent/
COPY tests/ tests/
COPY scripts/ scripts/
COPY config.example.toml ./

ENTRYPOINT ["python3", "-m", "sazed_agent"]
