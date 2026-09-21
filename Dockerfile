FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PIP_NO_CACHE_DIR=1 \
  DB_PATH=/data/bot.db

WORKDIR /app

RUN apt-get update && apt-get install -y \
  --no-install-recommends \
    tzdata \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN pip install -r requirements.lock

COPY bot ./bot

RUN useradd -r -u 10001 app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]

CMD ["python", "-m", "bot.main"]
