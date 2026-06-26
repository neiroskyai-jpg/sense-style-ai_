FROM python:3.12-slim

WORKDIR /app

# системные зависимости для Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY scripts/ ./scripts/
COPY architecture/prompts/ ./architecture/prompts/

# ключ передаётся через переменную окружения на запуске, не в образ
ENV SENSE_MODE=dev

CMD ["python", "-m", "scripts.run_vision", "--help"]
