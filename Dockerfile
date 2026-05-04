FROM python:3.12-slim

# docker CLI for issuing reload/restart commands against the host docker socket
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY routegate ./routegate
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "routegate.main:app", "--host", "0.0.0.0", "--port", "8000"]
