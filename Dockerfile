# SiliconScout web app: React build stage + Python runtime. Render builds this on every push to main.
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY siliconscout/ siliconscout/
COPY api/ api/
COPY config/ config/
COPY data/ data/
COPY app.py README.md ./
COPY --from=web /web/dist web/dist
# Secrets come from the host's environment settings, never from a baked-in .env.
ENV SS_JOBS_DIR=/var/data/jobs
EXPOSE 8000
CMD ["sh", "-c", "mkdir -p \"$SS_JOBS_DIR\" && exec uvicorn api.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
