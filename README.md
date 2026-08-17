# DeepSeek CLI + API Server

CLI de terminal y servidor OpenAI-compatible para DeepSeek Chat, reverse-engineered del APK Android v2.3.6.

## Archivos

```
deepseek          CLI principal
server.py         API OpenAI-compatible (FastAPI)
pow_solver.mjs    Solver del challenge Proof-of-Work (WASM SHA3)
.env              Credenciales y token cacheado (nunca entra a la imagen Docker)
Dockerfile
docker-compose.yml
requirements.txt
.dockerignore
```

---

## CLI

### Instalación

```bash
# Dependencias Python
pip install requests websocket-client

# Dependencias del sistema (macOS)
brew install node ffmpeg opus

# Hacer ejecutable
chmod +x deepseek
# Opcional: poner en el PATH
ln -s $(pwd)/deepseek /usr/local/bin/deepseek
```

### Uso

```bash
deepseek "¿Cuál es la capital de Francia?"   # pregunta directa
deepseek                                       # lee de stdin (Ctrl+D para enviar)
echo "hola" | deepseek                         # pipe

deepseek -s "nuevo tema"                       # nueva sesión (descarta contexto anterior)
deepseek --model expert "razona esto"          # DeepSeek-R1 (thinking)
deepseek --r1 "razona esto"                    # alias de --model expert
deepseek --search "noticias de hoy"            # activa búsqueda web

deepseek --image foto.jpg "¿qué ves?"         # imagen / visión
deepseek --image doc.pdf "resume esto"         # PDF

deepseek --asr nota.mp3                        # transcribe audio → prompt
deepseek --asr nota.mp3 "traduce al inglés"   # transcribe + instrucción extra

deepseek --no-training                         # opt-out de entrenamiento
deepseek --training-on                         # reactivar
deepseek --logout                              # borrar token guardado
```

### Modelos

| Flag / valor         | Modelo DeepSeek | Capacidad          |
|----------------------|-----------------|--------------------|
| (por defecto)        | V3              | Chat general       |
| `--model expert` / `--r1` | R1         | Razonamiento, thinking visible en stderr |
| `--model vision`     | Vision          | Imágenes y PDFs    |
| Con `--image`        | Vision (auto)   | Se activa solo     |

### Autenticación

El CLI busca credenciales en este orden:

1. Variable de entorno `DEEPSEEK_TOKEN`
2. `DEEPSEEK_TOKEN` en `.env`
3. `~/.deepseek_token`
4. Auto-login con `EMAIL` + `PASSWORD` del `.env` (o `DEEPSEEK_EMAIL` / `DEEPSEEK_PASSWORD`)
5. Login interactivo (pide email y contraseña)

Al hacer login guarda el token en `~/.deepseek_token` **y** en `DEEPSEEK_TOKEN=` dentro de `.env`.

### `.env`

```env
EMAIL=tu@email.com
PASSWORD=tu_password
DEEPSEEK_TOKEN=          # se rellena automáticamente al primer login
```

### ASR (voz → texto)

Usa el WebSocket propio de DeepSeek (`/api/v0/asr/ws`), no Whisper ni ningún servicio externo.

Requiere:
- `ffmpeg` — convierte cualquier formato de audio a PCM 16 kHz mono
- `libopus` — codifica los frames a Opus (lo que el servidor DeepSeek acepta)

El `audio_id` devuelto por el ASR se pasa también al chat, vinculando la entrada de voz con la conversación.

---

## API Server (OpenAI-compatible)

Permite usar DeepSeek con **cualquier cliente que hable OpenAI**: Open WebUI, Cursor, Continue, LangChain, etc.

### Arrancar sin Docker

```bash
pip install -r requirements.txt
python server.py
# Escucha en http://localhost:8000
```

### Arrancar con Docker

```bash
docker compose up --build
```

La imagen **no contiene credenciales**. El compose las inyecta en runtime desde tu `.env` local.

### Endpoints

#### `GET /v1/models`

```bash
curl http://localhost:8000/v1/models
```

#### `POST /v1/auth/login`

Obtiene un token y lo guarda. Si el email es distinto al del `.env`, actualiza las credenciales almacenadas.

```bash
curl http://localhost:8000/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"tu@email.com","password":"tu_password"}'
```

```json
{
  "token": "GFI1mg...",
  "token_type": "Bearer",
  "credentials_updated": false,
  "account": "tu@email.com",
  "usage": "Authorization: Bearer GFI1mg..."
}
```

`credentials_updated: true` indica que se guardó un email/password distinto al que había.

#### `POST /v1/chat/completions`

Compatible con el formato OpenAI. Soporta streaming.

```bash
# Sin streaming
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $DEEPSEEK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "hola"}]
  }'

# Con streaming
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $DEEPSEEK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "hola"}],
    "stream": true
  }'
```

Si el servidor arrancó con credenciales en `.env`, el header `Authorization` es opcional — el servidor usa su propio token.

#### `POST /v1/audio/transcriptions`

Misma interfaz que Whisper de OpenAI.

```bash
curl http://localhost:8000/v1/audio/transcriptions \
  -H "Authorization: Bearer $DEEPSEEK_TOKEN" \
  -F file=@nota.mp3 \
  -F model=whisper-1
```

```json
{"text": "el texto transcrito"}
```

### Modelos disponibles

| `model`              | Equivale a      |
|----------------------|-----------------|
| `deepseek-chat`      | V3 (default)    |
| `deepseek-v3`        | V3              |
| `gpt-3.5-turbo`      | V3              |
| `gpt-4o-mini`        | V3              |
| `deepseek-reasoner`  | R1 (thinking)   |
| `deepseek-r1`        | R1              |
| `o1`, `o1-mini`      | R1              |
| `deepseek-vision`    | Vision          |

### Autenticación del servidor

El servidor resuelve el token en este orden por request:

1. `Authorization: Bearer <token>` en el header del request (override por request)
2. Token en memoria (cacheado desde el inicio)
3. Auto-login desde `.env` / variables de entorno

#### Renovación automática del token

El token de DeepSeek es opaco (no JWT) — la duración real es desconocida pero se estima en días.

Estrategia doble:
- **Reactiva**: si cualquier request devuelve `401`, renueva el token y reintenta automáticamente sin que el cliente lo note.
- **Preventiva**: un watcher en background valida el token cada `TOKEN_REFRESH_INTERVAL` segundos (default: 6 horas) mientras el servidor esté idle.

### Variables de entorno del servidor

| Variable                 | Default | Descripción                                      |
|--------------------------|---------|--------------------------------------------------|
| `PORT`                   | `8000`  | Puerto de escucha                                |
| `TOKEN_REFRESH_INTERVAL` | `21600` | Segundos entre validaciones del watcher (6 h)    |
| `DEEPSEEK_TOKEN`         | —       | Token directo (evita auto-login al arrancar)     |
| `DEEPSEEK_EMAIL`         | —       | Email para auto-login                            |
| `DEEPSEEK_PASSWORD`      | —       | Password para auto-login                         |

### Configurar Open WebUI / Cursor / cualquier cliente OpenAI

```
Base URL:  http://localhost:8000/v1
API Key:   (el token de DeepSeek, o cualquier valor si el servidor ya tiene .env)
Model:     deepseek-chat  |  deepseek-reasoner  |  deepseek-vision
```

---

## Detalles técnicos

### Proof-of-Work

DeepSeek protege `/chat/completion` y `/file/upload_file` con un sistema PoW propio (`DeepSeekHashV1`): el servidor da un challenge, el cliente resuelve un hash SHA3 con dificultad variable. `pow_solver.mjs` lo resuelve usando el mismo WASM que la app Android.

### Protocolo ASR WebSocket

```
wss://chat.deepseek.com/api/v0/asr/ws?format=opus

Servidor → {"type":"start","audio_id":"<uuid>"}
Cliente  → [frames Opus binarios, 20ms c/u, 16kHz mono]
Cliente  → {"type":"finish","finish_reason":"done"}
Servidor → {"type":"result","result":"texto transcrito"}
Servidor → {"type":"end","code":0}
```

- Encoding: Opus, 16 kHz, 1 canal, APPLICATION_VOIP (2048), frames de 320 muestras
- `format=pcm` siempre devuelve `code:4 invalid_input` — solo funciona `format=opus`
- El `audio_id` se puede pasar a `/chat/completion` para vincular voz y chat

### Seguridad Docker

- `.env` nunca entra a la imagen (`.dockerignore`)
- Las credenciales se inyectan en runtime vía variables de entorno o volumen montado
- El token renovado se persiste en el `.env` montado y en `~/.deepseek_token` del host
