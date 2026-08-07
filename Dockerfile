# Builds for WeChat Cloud Run (微信云托管) and any other container platform.
#
#   docker build -t open-digest .
#   docker run -p 8080:8080 -e OPENROUTER_API_KEY=... open-digest
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    DIGEST_DB=/data/digest.db

WORKDIR /app

COPY requirements.txt requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-api.txt

COPY *.py catalog.yaml ./

# Cloud Run gives the container an ephemeral filesystem. Mount a volume at
# /data, or point DIGEST_DB at a managed database — otherwise every user's
# subscriptions vanish on redeploy.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; \
      urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"

# 2 workers x 4 threads: the read path is IO-bound on SQLite, not CPU-bound.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "60", "--access-logfile", "-", "api:app"]
