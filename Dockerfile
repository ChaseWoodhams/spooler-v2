FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY printers/ printers/
COPY public/ public/

# Runtime data lives in /data (mounted as volume)
ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8080 8443 8765 8766

CMD ["python3", "server.py"]
