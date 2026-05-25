FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HERTFORD_CONFIG=/config/rooms.yaml

WORKDIR /app

# Copy only what's needed to install the package.
COPY pyproject.toml ./
COPY hertford ./hertford

RUN pip install ".[api]"

# Config is mounted at /config in compose; the app reads /config/rooms.yaml.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "hertford.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
