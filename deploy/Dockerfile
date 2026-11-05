FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 RADAR_DATA_DIR=/data TZ=Europe/Moscow
ENV RADAR_SUPERVISOR=docker HOST=0.0.0.0 NICEGUI_STORAGE_PATH=/data/nicegui
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt \
    && groupadd -g 10001 radar && useradd -u 10001 -g radar -M radar \
    && mkdir /data && chown radar:radar /data
COPY --chown=radar:radar . .
USER radar
EXPOSE 8080
HEALTHCHECK --start-period=120s --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=8)"
CMD ["sh", "-c", "umask 077; python tools/preflight.py && exec python run.py"]
