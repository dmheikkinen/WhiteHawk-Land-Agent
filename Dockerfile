# Ohio Landman Pipeline — Docker image
#
# Build:
#   docker build -t ohio-landman .
#
# Run (reads secrets from .env, persists output to ./data):
#   docker run --rm -p 5000:5000 --env-file .env -v $(pwd)/data:/app/data ohio-landman
#
# Or with Docker Compose:
#   docker compose up -d

FROM python:3.11-slim

# System deps: curl for HEALTHCHECK
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first — layer is cached unless requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Application code
COPY server.py \
     harvest_contacts.py \
     score_contacts.py \
     run_ohio_landman_assistant.py \
     ./

# Templates (Jinja2)
COPY templates/ templates/

# Data directory — mount a host volume here to persist output files
RUN mkdir -p data

# Run as non-root for security
RUN useradd -m -u 1000 landman \
    && chown -R landman:landman /app
USER landman

EXPOSE 5000

# Liveness check — confirms the web UI is responding
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:5000/setup || exit 1

# Single worker — pipeline scripts are subprocess-based, not thread-safe for multi-worker
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "5000", "--workers", "1"]
