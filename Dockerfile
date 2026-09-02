FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BIQ_LOG_JSON=true

RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk tini && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e .

COPY configs ./configs
COPY data/raw ./data/raw
COPY docs ./docs
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

RUN useradd -m -u 10001 biq && mkdir -p /app/state /app/reports && chown -R biq:biq /app
USER biq
VOLUME ["/app/state", "/app/reports"]

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "btc_inventory_quant"]
CMD ["shadow", "--config", "configs/shadow.yaml"]
