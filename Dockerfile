# Single-container FireProtect — the deploy target for Render / Railway /
# Fly.io / Hugging Face Spaces, which all run exactly one process per service.
#
# This bundles the backend + the built dashboard + the trained model into one
# image listening on $PORT. It does NOT bundle an MQTT broker: cloud platforms
# expose one HTTP port, and a broker needs its own TCP port. Point the app at a
# managed broker (HiveMQ Cloud has a free tier) with MQTT_HOST/MQTT_PORT, or
# leave it unset and POST to /api/telemetry over HTTPS.
#
# The dashboard is NOT compiled here. `frontend/dist` is committed to the
# repository (see .gitignore), so the image copies the build that was already
# produced and tested locally. That removes Node, npm and the whole TypeScript
# toolchain from the deploy path: the build is faster, reproducible, and cannot
# fail on a toolchain difference between a laptop and the build host. Rebuild
# with `cd frontend && npm run build` and commit the result when the UI changes;
# `run_local.py` checks mtimes and rebuilds automatically, so it cannot go
# stale unnoticed.
#
# For the full local stack including Mosquitto, use docker-compose.yml instead.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/app ./backend/app
COPY ml/random_forest.joblib ml/decision_tree.joblib ml/feature_columns.json ./ml/
COPY frontend/dist ./frontend/dist

# Demo mode imports the simulator's physics rather than duplicating it, so the
# module has to be in the image. It is small and has no dependencies.
COPY simulator/virtual_device.py ./simulator/virtual_device.py

# Data lives on a mounted volume where the platform provides one; otherwise it
# is ephemeral and resets on redeploy, which is fine for a demo.
RUN mkdir -p /data && useradd --create-home --uid 10001 fireprotect \
    && chown -R fireprotect:fireprotect /app /data
USER fireprotect

ENV PYTHONPATH=/app/backend \
    MODEL_DIR=/app/ml \
    FRONTEND_DIST=/app/frontend/dist \
    DATABASE_URL=sqlite:////data/fireprotect.db \
    THINGSPEAK_BUFFER_PATH=/data/thingspeak_buffer.jsonl \
    PORT=8000

EXPOSE 8000

# Binding 0.0.0.0 is required inside a container — the platform's router
# reaches us over the container network, and loopback would be unreachable.
# The container is the security boundary; only $PORT is published.
#
# $PORT is injected by Render/Railway/Fly/Spaces, so it is read at runtime via
# a shell rather than baked into an exec-form CMD.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir /app/backend"]
