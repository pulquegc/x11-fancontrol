FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ipmitool smartmontools \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/

ENV DATA_DIR=/data \
    PORT=8765 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8765

STOPSIGNAL SIGTERM
CMD ["python3", "/app/fancontrol.py"]
