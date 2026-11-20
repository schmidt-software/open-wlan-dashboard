# WLAN Dashboard: dashboard and collector in one image, see compose.yaml.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    DASHBOARD_BIND=0.0.0.0

WORKDIR /app
# code only; .dockerignore keeps .env, history and other private files out of the image
COPY . .
RUN python3 dashboard.py --selftest \
 && useradd --system --uid 10001 --no-create-home app \
 && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8765
CMD ["python3", "dashboard.py"]
