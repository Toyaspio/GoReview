FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /app
COPY pyproject.toml ./
COPY goreview ./goreview
RUN pip install --no-cache-dir . && python -m playwright install --with-deps chromium && \
    apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --uid 10001 --create-home worker && mkdir /data && chown worker:worker /data
ENV GOREVIEW_DATA=/data
USER worker
CMD ["python", "-m", "uvicorn", "goreview.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8097", "--workers", "1", "--no-access-log"]
