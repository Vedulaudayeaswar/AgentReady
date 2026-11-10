# Single-container image for Render (free plan): builds the React frontend and serves it from the
# FastAPI process, with background jobs running inline (no Redis / Celery worker needed).
# Build context: repository root. Used by render.yaml.
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 STATIC_DIR=/app/static
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/ .
COPY --from=web /web/dist /app/static
RUN rm -f agentready.db && sed -i 's/\r$//' docker/*.sh && chmod +x docker/*.sh \
    && useradd --create-home --uid 10001 agentready && chown -R agentready /app
USER agentready
EXPOSE 10000
CMD ["sh", "docker/start-api.sh"]
