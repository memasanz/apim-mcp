# syntax=docker/dockerfile:1.7

# ---- Stage 1: build the React admin UI ----
FROM node:20-alpine AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# ---- Stage 2: runtime (Python + Azure CLI) ----
# Use python:slim and install `az` so we have a predictable Python + pip
# environment. (mcr.microsoft.com/azure-cli was Alpine in older tags and
# Mariner in newer ones, neither ships pip on PATH out of the box.)
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    CONFIG_PATH=/etc/cli-mcp/config.json \
    LOG_LEVEL=INFO \
    DEBIAN_FRONTEND=noninteractive

# Install the Azure CLI from the Microsoft apt repo.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg apt-transport-https lsb-release \
 && mkdir -p /etc/apt/keyrings \
 && curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
 && AZ_DIST="$(lsb_release -cs)" \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ ${AZ_DIST} main" \
      > /etc/apt/sources.list.d/azure-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends azure-cli \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching).
COPY server/pyproject.toml ./server/pyproject.toml
RUN pip install --upgrade pip \
 && pip install \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.30" \
      "mcp>=1.2.0" \
      "pydantic>=2.7" \
      "pydantic-settings>=2.4" \
      "azure-identity>=1.17" \
      "httpx>=0.27" \
      "python-jose[cryptography]>=3.3" \
      "watchfiles>=0.22"

# Copy server source and install as a package.
COPY server/ ./server/
RUN pip install --no-deps -e ./server

# Drop the built UI in the place FastAPI serves it from.
COPY --from=ui-build /server/app/static ./server/app/static

# Default config (overridable via volume mount on /etc/cli-mcp/config.json).
RUN mkdir -p /etc/cli-mcp
COPY config/config.sample.json /etc/cli-mcp/config.json

EXPOSE 8000
WORKDIR /app/server

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:${PORT}/healthz || exit 1

CMD ["python", "-m", "app.main"]
