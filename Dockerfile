FROM python:3.09-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY . .

RUN chmod -R 777 /app

CMD ["python", "server.py"]
