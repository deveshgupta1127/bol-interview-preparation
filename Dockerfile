# Bol API image for Azure Container Apps. Build: docker build -t bol-api .
# The web/ folder is not in the image: Static Web Apps serves it.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SERVE_WEB=false

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-compile -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

# Run as a normal user, not root. /srv/data is the only folder it can write to.
RUN useradd --system --uid 10001 --no-create-home bol && mkdir /srv/data && chown bol /srv/data
USER bol

# For trying the image locally: SQLite in /srv/data (gone when the container is removed, unless you mount a
# volume there). On Azure, DATABASE_URL points at PostgreSQL and replaces this; the app refuses SQLite there.
ENV DATABASE_URL=sqlite:////srv/data/bol.db

EXPOSE 8000
# Container Apps ingress ends TLS and forwards; --proxy-headers keeps the client's scheme and address.
# One worker per replica: scaling happens by adding replicas. Long Claude/Prisma waits run in the thread pool.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
