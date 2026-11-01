# Bol API image for Azure Container Apps. Build: docker build -t bol-api .
# The web/ folder is not in the image: Static Web Apps serves it. The API stores nothing, so it needs no volume.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SERVE_WEB=false

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-compile -r requirements.txt

COPY app ./app

# Run as a normal user, not root, with nowhere to write.
RUN useradd --system --uid 10001 --no-create-home bol
USER bol

EXPOSE 8000
# Container Apps ingress ends TLS and forwards; --proxy-headers keeps the client's scheme and address.
# One worker: Gnani allows one request at a time per key, so one process keeps the queue in one place.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
