FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine.py app.py entrypoint.py scoring_constants.py default_config.json ./
COPY templates/ templates/

RUN mkdir -p /config

ENV MEDIAREDUCER_CONFIG=/config/config.json
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# The UI polls /api/status constantly, so it doubles as the health probe.
# Pure-Python probe — the slim image ships no curl/wget.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/status', timeout=8)"]

# Optional PUID/PGID user mapping (see entrypoint.py); root without them.
ENTRYPOINT ["python3", "entrypoint.py"]
