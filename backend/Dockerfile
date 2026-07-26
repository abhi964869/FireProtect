# FireProtect backend.
#
# Build context is the REPO ROOT (not backend/) because the service needs the
# trained model artefacts from ml/ at runtime.
#
#   docker build -f backend/Dockerfile -t fireprotect-backend .

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so a source change does not invalidate this layer.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/app /app/backend/app
# Model artefacts: the random forest, the feature order, and the training
# scripts (so `python ml/train.py` can be re-run inside the container).
COPY ml/random_forest.joblib ml/decision_tree.joblib ml/feature_columns.json /app/ml/
COPY ml/generate_dataset.py ml/export_tree.py ml/train.py /app/ml/

# SQLite and the ThingSpeak buffer live on a volume so data survives restarts.
RUN mkdir -p /data && useradd --create-home --uid 10001 fireprotect \
    && chown -R fireprotect:fireprotect /app /data
USER fireprotect

ENV PYTHONPATH=/app/backend \
    DATABASE_URL=sqlite:////data/fireprotect.db \
    MODEL_DIR=/app/ml \
    THINGSPEAK_BUFFER_PATH=/data/thingspeak_buffer.jsonl

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
