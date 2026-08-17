# DeepSeek CLI + API Server

CLI de terminal y servidor OpenAI-compatible para DeepSeek Chat, reverse-engineered del APK Android v2.3.6.

## Archivos

```
deepseek              CLI principal
server.py             API OpenAI-compatible (FastAPI)
pow_solver.mjs        Solver del challenge Proof-of-Work (WASM SHA3)
sha3_wasm_bg.wasm     WASM compilado requerido por pow_solver.mjs
.env                  Credenciales y token cacheado (nunca entra a la imagen Docker)
Dockerfile
docker-compose.yml
requirements.txt
.dockerignore
```

---

## Autenticación — cómo funciona

Todo el sistema gira alrededor de un único token de sesión que DeepSeek emite al hacer login. Ese token es opaco (no es un JWT, no tiene fecha de expiración decodificable) y en la práctica dura días.

### Dónde viven las credenciales

```
.env                        ← fuente de verdad para credenciales y token cacheado
~/.deepseek_token           ← copia del token (backup)
```

El archivo `.env` tiene tres variables:

```env
EMAIL=tu@email.com          ← email de DeepSeek
PASSWORD=tu_password        ← contraseña
DEEPSEEK_TOKEN=             ← se rellena automáticamente al primer login; no tocar a mano
```

### Flujo de login — CLI

El CLI resuelve el token en este orden cada vez que arranca:

```
1. Variable de entorno DEEPSEEK_TOKEN     → úsalo directo
2. DEEPSEEK_TOKEN en .env                 → úsalo directo
3. ~/.deepseek_token                      → úsalo directo
4. EMAIL + PASSWORD del .env              → auto-login silencioso, guarda token
5. Variables DEEPSEEK_EMAIL / DEEPSEEK_PASSWORD → auto-login silencioso
6. Prompt interactivo                     → pide email y contraseña en terminal
```

Cuando el CLI hace login (pasos 4-6), guarda el token nuevo en **ambos sitios**: `DEEPSEEK_TOKEN` en `.env` y en `~/.deepseek_token`. En el siguiente arranque ya no necesita hacer login.

Para borrar la sesión y forzar un login nuevo:

```bash
deepseek --logout
```

### Flujo de login — Servidor

El servidor resuelve el token en dos momentos:

**Al arrancar:**
```
1. DEEPSEEK_TOKEN env var                 → token listo, sin login
2. DEEPSEEK_TOKEN en .env montado         → token listo, sin login
3. ~/.deepseek_token montado              → token listo, sin login
4. DEEPSEEK_EMAIL + DEEPSEEK_PASSWORD     → auto-login, guarda token
5. EMAIL + PASSWORD en .env montado       → auto-login, guarda token
```

**Durante requests:**
- Si el header `Authorization: Bearer <token>` está presente → usa ese token para ese request específico (override).
- Si no hay header → usa el token global del servidor.
- Si DeepSeek devuelve `401` → renueva el token automáticamente y reintenta. El cliente no nota nada.

**Watcher en background:** cada 6 horas (configurable) valida el token con `GET /users/current`. Si expiró, hace re-login con las credenciales del `.env`. Sirve para servidores que están idle y no reciben requests.

### Cambiar de cuenta

El endpoint `/v1/auth/login` tiene esta lógica:

- **Mismo email que en `.env`** → solo refresca el token. Las credenciales almacenadas no cambian.
- **Email distinto** → actualiza `EMAIL` y `PASSWORD` en `.env` con la nueva cuenta, y guarda el nuevo token.

---

## Docker — cómo inyectar las variables

La imagen Docker **no contiene credenciales**. El `.env` está en `.dockerignore` y nunca entra al build. Las credenciales se inyectan en runtime de dos formas, que pueden combinarse:

### Opción A — Volumen (recomendada)

El `docker-compose.yml` ya lo hace automáticamente: monta tu `.env` local dentro del contenedor y lee las variables de él.

```bash
# Solo necesitas tener el .env en la misma carpeta que el docker-compose.yml
# y arrancar:
docker compose up --build
```

El compose lee tu `.env` del host y hace dos cosas con él:

1. **Lo pasa como variables de entorno** al contenedor (`DEEPSEEK_EMAIL`, `DEEPSEEK_PASSWORD`, `DEEPSEEK_TOKEN`).
2. **Lo monta como archivo** en `/app/.env` dentro del contenedor, para que el watcher pueda escribir el token renovado y persista en tu host.

```yaml
# docker-compose.yml (resumen)
environment:
  - DEEPSEEK_EMAIL=${EMAIL}
  - DEEPSEEK_PASSWORD=${PASSWORD}
  - DEEPSEEK_TOKEN=${DEEPSEEK_TOKEN:-}
volumes:
  - ./.env:/app/.env           # lectura y escritura — el token renovado persiste
  - ~/.deepseek_token:/root/.deepseek_token
```

### Opción B — Variables de entorno directas

Sin montar archivos, pasando todo inline. Útil en CI, Kubernetes, Railway, Render, etc.

```bash
docker run -p 8000:8000 \
  -e DEEPSEEK_EMAIL=tu@email.com \
  -e DEEPSEEK_PASSWORD=tu_password \
  deepseek-deepseek
```

En este modo el token renovado **no persiste** al reiniciar el contenedor (no hay archivo montado), pero el auto-login lo regenera solo al arrancar.

### Opción C — Token directo (sin credenciales)

Si ya tienes el token y no quieres guardar el password en ningún lado:

```bash
docker run -p 8000:8000 \
  -e DEEPSEEK_TOKEN=GFI1mgTYdj... \
  deepseek-deepseek
```

El servidor lo usa directamente. Si expira, no puede renovarlo solo (no tiene credenciales). Para renovar, llama al endpoint de login.

### Obtener el token por primera vez

Si el `.env` tiene `EMAIL` y `PASSWORD`, el servidor se loguea solo al arrancar y ya. Si quieres hacerlo manualmente:

```bash
# Con el servidor corriendo:
curl http://localhost:8000/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"tu@email.com","password":"tu_password"}'
```

Respuesta:

```json
{
  "token": "GFI1mgTYdj...",
  "token_type": "Bearer",
  "credentials_updated": false,
  "account": "tu@email.com",
  "usage": "Authorization: Bearer GFI1mgTYdj..."
}
```

El servidor guarda el token automáticamente. `credentials_updated: true` indica que se sobreescribieron `EMAIL`/`PASSWORD` porque el email era distinto al que había.

---

## CLI

### Instalación

```bash
# Dependencias Python
pip install requests websocket-client

# Dependencias del sistema (macOS)
brew install node ffmpeg opus

chmod +x deepseek
ln -s $(pwd)/deepseek /usr/local/bin/deepseek   # opcional: agregar al PATH
```

### Uso

```bash
deepseek "¿Cuál es la capital de Francia?"
deepseek                                       # lee de stdin (Ctrl+D para enviar)
echo "hola" | deepseek

deepseek -s "nuevo tema"                       # nueva sesión
deepseek --model expert "razona esto"          # DeepSeek-R1 (thinking)
deepseek --r1 "razona esto"                    # alias de --model expert
deepseek --search "noticias de hoy"            # búsqueda web

deepseek --image foto.jpg "¿qué ves?"
deepseek --image doc.pdf "resume esto"

deepseek --asr nota.mp3                        # voz → texto → chat
deepseek --asr nota.mp3 "traduce al inglés"   # voz + instrucción extra

deepseek --no-training                         # opt-out de entrenamiento
deepseek --logout                              # borrar token
```

### Modelos

| Flag / valor              | Modelo DeepSeek | Capacidad                              |
|---------------------------|-----------------|----------------------------------------|
| (por defecto)             | V3              | Chat general                           |
| `--model expert` / `--r1` | R1              | Razonamiento (thinking en stderr)      |
| `--model vision`          | Vision          | Imágenes y PDFs                        |
| Con `--image`             | Vision (auto)   | Se activa automáticamente              |

---

## API Server (OpenAI-compatible)

Permite usar DeepSeek con cualquier cliente que hable OpenAI: Open WebUI, Cursor, Continue, LangChain, etc.

### Arrancar sin Docker

```bash
pip install -r requirements.txt
python server.py
# http://localhost:8000
```

### Arrancar con Docker

```bash
docker compose up --build
```

### Endpoints

#### `GET /v1/models`

```bash
curl http://localhost:8000/v1/models
```

#### `POST /v1/auth/login`

```bash
curl http://localhost:8000/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"tu@email.com","password":"tu_password"}'
```

#### `POST /v1/chat/completions`

```bash
# Sin streaming
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hola"}]}'

# Con streaming
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hola"}],"stream":true}'
```

El header `Authorization: Bearer <token>` es opcional si el servidor arrancó con `.env`.

#### `POST /v1/audio/transcriptions`

```bash
curl http://localhost:8000/v1/audio/transcriptions \
  -F file=@nota.mp3 \
  -F model=whisper-1
```

```json
{"text": "el texto transcrito"}
```

### Modelos disponibles

| `model`             | Equivale a    |
|---------------------|---------------|
| `deepseek-chat`     | V3 (default)  |
| `deepseek-v3`       | V3            |
| `gpt-3.5-turbo`     | V3            |
| `gpt-4o-mini`       | V3            |
| `deepseek-reasoner` | R1 (thinking) |
| `deepseek-r1`       | R1            |
| `o1`, `o1-mini`     | R1            |
| `deepseek-vision`   | Vision        |

### Configurar Open WebUI / Cursor / cualquier cliente OpenAI

```
Base URL:  http://localhost:8000/v1
API Key:   (dejar vacío, o poner el token si no hay .env configurado)
Model:     deepseek-chat  |  deepseek-reasoner  |  deepseek-vision
```

### Variables de entorno del servidor

| Variable                 | Default | Descripción                                   |
|--------------------------|---------|-----------------------------------------------|
| `PORT`                   | `8000`  | Puerto de escucha                             |
| `TOKEN_REFRESH_INTERVAL` | `21600` | Segundos entre chequeos del watcher (6 h)     |
| `DEEPSEEK_TOKEN`         | —       | Token directo (skip auto-login al arrancar)   |
| `DEEPSEEK_EMAIL`         | —       | Email para auto-login                         |
| `DEEPSEEK_PASSWORD`      | —       | Password para auto-login                      |

---

## Detalles técnicos

### Proof-of-Work

DeepSeek protege `/chat/completion` y `/file/upload_file` con un sistema PoW propio (`DeepSeekHashV1`): el servidor emite un challenge, el cliente resuelve un hash SHA3 con dificultad variable. `pow_solver.mjs` lo resuelve usando el mismo WASM que la app Android (`sha3_wasm_bg.wasm`).

### ASR (voz → texto)

Usa el WebSocket propio de DeepSeek, no Whisper. Requiere `ffmpeg` y `libopus`.

```
wss://chat.deepseek.com/api/v0/asr/ws?format=opus

Servidor → {"type":"start","audio_id":"<uuid>"}
Cliente  → [frames Opus binarios, 20ms c/u, 16kHz mono]
Cliente  → {"type":"finish","finish_reason":"done"}
Servidor → {"type":"result","result":"texto transcrito"}
Servidor → {"type":"end","code":0}
```

- Solo funciona `format=opus` (`format=pcm` siempre devuelve `code:4`)
- El `audio_id` se pasa a `/chat/completion` para vincular voz y conversación
