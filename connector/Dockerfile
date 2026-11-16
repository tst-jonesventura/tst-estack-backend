FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY connector/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --only-binary :all: --require-hashes -r /tmp/requirements.lock

COPY src/database_access /app/database_access
COPY src/utils/__init__.py src/utils/log.py src/utils/ttl_cache.py /app/utils/
COPY connector/estack_connector /app/estack_connector

RUN useradd --system --uid 10001 --no-create-home connector
USER connector

ENTRYPOINT ["python", "-m", "estack_connector"]
