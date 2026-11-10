FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# Optional JavaScript rendering: docker compose build --build-arg WITH_PLAYWRIGHT=1
ARG WITH_PLAYWRIGHT=0
COPY requirements-playwright.txt .
RUN if [ "$WITH_PLAYWRIGHT" = "1" ]; then pip install -r requirements-playwright.txt && python -m playwright install --with-deps chromium; fi
COPY . .
# strip CR in case the scripts were checked out with Windows line endings
RUN sed -i 's/\r$//' docker/*.sh && chmod +x docker/*.sh \
    && useradd --create-home --uid 10001 agentready && chown -R agentready /app
USER agentready
EXPOSE 8000
# Liveness only (process answers HTTP). Dependency readiness is GET /health.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"
CMD ["sh", "docker/start-api.sh"]
