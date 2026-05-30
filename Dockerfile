# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    BOOKREC_DB_PATH=/app/data/library.db

WORKDIR /app

# Install dependencies first so the heavy scikit-learn/scipy wheels stay cached
# across code edits.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the rest of the application.
COPY server/ ./server/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/

# Persisted SQLite DB lives here; mount a volume on this path to keep it across
# container restarts. Created up front so the non-root user can write to it.
RUN mkdir -p /app/data && \
    useradd --create-home --shell /bin/bash bookrec && \
    chown -R bookrec:bookrec /app

USER bookrec

EXPOSE 8000

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
