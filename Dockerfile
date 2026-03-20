FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY polymarket_index/ polymarket_index/
COPY .env.example .env.example

RUN mkdir -p logs

ENV PYTHONUNBUFFERED=1

# Default: dry-run edge trader
CMD ["python3", "-m", "polymarket_index.edge"]
