# FireProtect virtual device.
#
# Build context is the REPO ROOT: the simulator imports the sensor physics from
# ml/generate_dataset.py so it produces telemetry from the same distribution
# the models were trained on.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir "paho-mqtt>=2.0,<3.0" "numpy>=1.26,<3.0" \
    "pandas>=2.2,<3.0" "httpx>=0.27,<1.0"

COPY simulator/virtual_device.py /app/simulator/
COPY ml/generate_dataset.py /app/ml/

RUN useradd --create-home --uid 10002 simulator
USER simulator

CMD ["python", "simulator/virtual_device.py", "--host", "mosquitto", "--scenario", "normal"]
