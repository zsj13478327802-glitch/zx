FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 ZX_CLOUD_MODE=1 ZX_DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py engine.py ./
COPY static/ ./static/
COPY assets/ ./assets/
RUN mkdir -p /app/data
EXPOSE 8080
CMD ["sh","-c","exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
