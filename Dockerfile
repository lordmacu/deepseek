FROM python:3.11-slim

# Node.js 18 (repos de Debian Bookworm), ffmpeg, libopus
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs \
        ffmpeg \
        libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps primero (capa cacheada)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App — sha3_wasm_bg.wasm es requerido por pow_solver.mjs en runtime
COPY deepseek pow_solver.mjs sha3_wasm_bg.wasm server.py ./

RUN chmod +x /app/deepseek

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
