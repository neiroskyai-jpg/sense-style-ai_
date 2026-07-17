FROM python:3.12-slim

WORKDIR /app

# системные зависимости для Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY architecture/prompts/ ./architecture/prompts/
COPY architecture/reference/ ./architecture/reference/
# статика сайта: лендинг, квиз, карточки стилей — app.main отдаёт web/ как static_folder
COPY web/ ./web/
# RAG-индекс: chunks.json нужен и без fastembed (фолбэк на теги), vectors.npz — для семантики
COPY data/rag/ ./data/rag/
# каталог реальных вещей: без него конструктор гардероба вырождается в текст вместо фото
COPY data/fashion-base/ ./data/fashion-base/
# детектор лица YuNet: без него кроп головы деградирует до эвристики, лицо в референсе остаётся
# мелким — и модель «додумывает» чужое вместо лица клиентки
COPY data/models/ ./data/models/

# OPENROUTER_API_KEY задаётся переменной окружения на хосте (не в образ)
ENV SENSE_MODE=dev
ENV SENSE_DATA_DIR=/data
EXPOSE 80

# веб-сервис; порт берётся из $PORT (Amvera/облако), по умолчанию 80
CMD ["sh", "-c", "gunicorn app.main:app --timeout 300 --workers 1 --bind 0.0.0.0:${PORT:-80}"]
