FROM python:3.11-slim

# System deps: Node.js 20, ffmpeg, libopus
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
        ffmpeg \
        libopus0 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App files — NO incluir .env (credenciales se inyectan en runtime vía volumen o env vars)
COPY deepseek pow_solver.mjs server.py ./

# Make CLI executable
RUN chmod +x /app/deepseek

# Default: run the API server
# Override with: docker run ... python /app/deepseek "hello"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]

EXPOSE 8000
