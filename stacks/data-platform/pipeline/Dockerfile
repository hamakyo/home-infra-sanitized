FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    DLT_DATA_DIR=/tmp/dlt \
    DBT_PROFILES_DIR=/app/dbt

WORKDIR /app

COPY pipeline/requirements.txt /app/pipeline/requirements.txt
RUN pip install --no-cache-dir -r /app/pipeline/requirements.txt

COPY pipeline/ /app/pipeline/
COPY dbt/ /app/dbt/
COPY rag/ /app/rag/

RUN chmod +x /app/pipeline/entrypoint.sh

ENTRYPOINT ["/app/pipeline/entrypoint.sh"]
