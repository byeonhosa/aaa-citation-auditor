# syntax=docker/dockerfile:1
FROM python:3.12-slim

# System dependencies required for PyMuPDF and other compiled packages
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security
RUN groupadd --gid 1001 appgroup \
 && useradd --uid 1001 --gid appgroup --no-create-home --shell /bin/bash appuser

# Create the persistent data directory and give it to appuser
RUN mkdir -p /data && chown appuser:appgroup /data

WORKDIR /app

# Install Python dependencies first (leverages Docker layer cache)
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir ".[dev]"

# Copy the application source
COPY --chown=appuser:appgroup . .

# Copy and configure the entrypoint script
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Default environment – zero-config startup
ENV DATABASE_URL="sqlite:////data/aaa.db" \
    LOG_LEVEL="INFO" \
    AI_PROVIDER="none"

EXPOSE 8000

USER appuser

ENTRYPOINT ["/docker-entrypoint.sh"]
