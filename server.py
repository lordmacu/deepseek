#!/usr/bin/env python3
"""
OpenAI-compatible API server that proxies to DeepSeek.

Endpoints:
  GET  /v1/models
  POST /v1/chat/completions   (streaming + non-streaming)
  POST /v1/audio/transcriptions

Auth: pass your DeepSeek token as the Bearer API key.
  curl http://localhost:8000/v1/chat/completions \
    -H "Authorization: Bearer $DEEPSEEK_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hola"}]}'
"""

import os, sys, json, uuid, time, importlib.util, tempfile, asyncio, threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Header
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import requests as _requests

import capabilities

# Intervalo de validación del token en segundos (configurable por env).
# El token de DeepSeek es opaco (no JWT), duración desconocida pero probablemente días.
# La estrategia real de renovación es reactiva: si un request devuelve 401, se renueva
# y reintenta. El watcher es solo una red de seguridad para sesiones idle.
TOKEN_REFRESH_INTERVAL = int(os.environ.get("TOKEN_REFRESH_INTERVAL", "21600"))  # 6 h

# ── Load deepseek CLI as module ──────────────────────────────────────────────
# spec_from_file_location can't infer loader for files without .py extension;
# SourceFileLoader handles it explicitly.
import importlib.machinery as _imm

_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepseek")
_loader   = _imm.SourceFileLoader("ds", _CLI_PATH)
_spec     = importlib.util.spec_from_loader("ds", _loader)
_ds       = importlib.util.module_from_spec(_spec)
_loader.exec_module(_ds)

# ── Token manager ────────────────────────────────────────────────────────────

class _TokenManager:
    """
    Server-side token cache with auto-login from .env.

    Priority when resolving a token for a request:
      1. Bearer header from the request (per-request override)
      2. In-memory cached token (validated on first use)
      3. Auto-login with .env EMAIL/PASSWORD
    """

    def __init__(self):
        self._token: str | None = None
        self._lock  = threading.Lock()

    def _try_cached(self):
        """Load from file/env and validate."""
        t = _ds.load_token()
        if t and _ds._token_valid(t):
            return t
        return None

    def get(self, bearer=None):
        """Return a valid token, refreshing if necessary."""
        # Per-request override always wins
        if bearer:
            return bearer

        with self._lock:
            if self._token:
                return self._token

            # Try cached token from disk/.env
            t = self._try_cached()
            if t:
                self._token = t
                return t

            # Auto-login from .env credentials
            t = _ds._auto_login()
            if t:
                self._token = t
                return t

        raise HTTPException(
            401,
            detail=(
                "No hay token disponible. "
                "Usa POST /v1/auth/login o configura EMAIL+PASSWORD en .env, "
                "o pasa Authorization: Bearer <token> en el request."
            ),
        )

    def refresh(self) -> str:
        """Force re-login; called after a 401 from DeepSeek."""
        with self._lock:
            self._token = None
            t = _ds._auto_login()
            if t:
                self._token = t
                return t
        raise HTTPException(502, detail="Re-login falló: revisa EMAIL/PASSWORD en .env")

    def set(self, token: str):
        with self._lock:
            self._token = token


_tokens = _TokenManager()


# ── Background token refresh ─────────────────────────────────────────────────

async def _token_watcher():
    """
    Runs every TOKEN_REFRESH_INTERVAL seconds.
    Checks if the current token is still valid; re-logs in if not.
    Uses the credentials stored in .env (EMAIL / PASSWORD).
    """
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
        try:
            token = _tokens._token or _ds.load_token()
            valid = await loop.run_in_executor(
                None, lambda: bool(token and _ds._token_valid(token))
            )
            if not valid:
                print(
                    f"[token-watcher] Token expirado o ausente, renovando...",
                    file=sys.stderr,
                )
                await loop.run_in_executor(None, _tokens.refresh)
                print("[token-watcher] Token renovado.", file=sys.stderr)
        except Exception as exc:
            print(f"[token-watcher] Error: {exc}", file=sys.stderr)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    loop = asyncio.get_event_loop()
    # Try to get a valid token immediately so the first request doesn't block
    try:
        await loop.run_in_executor(None, lambda: _tokens.get())
        print("[startup] Token listo.", file=sys.stderr)
    except HTTPException:
        print(
            "[startup] Sin token disponible. "
            "Usa POST /v1/auth/login o configura EMAIL+PASSWORD en .env.",
            file=sys.stderr,
        )

    watcher = asyncio.create_task(_token_watcher())
    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    watcher.cancel()
    try:
        await watcher
    except asyncio.CancelledError:
        pass


# ── App ──────────────────────────────────────────────────────────────────────
APP_VERSION = "1.0.0"

app = FastAPI(title="DeepSeek OpenAI API", version=APP_VERSION, lifespan=lifespan)

# Model → DeepSeek model_type mapping
_MODEL_MAP: dict[str, str] = {
    # V3 / default
    "deepseek-chat":        "default",
    "deepseek-v3":          "default",
    "gpt-3.5-turbo":        "default",
    "gpt-4o-mini":          "default",
    # R1 / expert (thinking)
    "deepseek-reasoner":    "expert",
    "deepseek-r1":          "expert",
    "o1":                   "expert",
    "o1-mini":              "expert",
    "o3-mini":              "expert",
    # Vision
    "deepseek-vision":      "vision",
    "gpt-4-vision-preview": "vision",
    "gpt-4o":               "default",  # default; attach files separately
}

_MODELS = [
    {"id": "deepseek-chat",     "object": "model", "created": 0, "owned_by": "deepseek"},
    {"id": "deepseek-reasoner", "object": "model", "created": 0, "owned_by": "deepseek"},
    {"id": "deepseek-vision",   "object": "model", "created": 0, "owned_by": "deepseek"},
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bearer(authorization):
    """Extract raw token from 'Authorization: Bearer ...' header, or None."""
    if authorization and authorization.startswith("Bearer "):
        v = authorization[7:].strip()
        return v if v else None
    return None


def _auth_headers(token: str) -> dict:
    return {**_ds.HEADERS, "Authorization": f"Bearer {token}"}


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _format_messages(messages: list) -> str:
    """Convert OpenAI messages array into a single DeepSeek prompt."""
    system = next(
        (_extract_text(m.get("content", "")) for m in messages if m.get("role") == "system"),
        None,
    )
    non_system = [m for m in messages if m.get("role") != "system"]

    # Single user message — most common case
    if len(non_system) == 1 and non_system[0].get("role") == "user":
        text = _extract_text(non_system[0].get("content", ""))
        return f"[System]: {system}\n\n{text}" if system else text

    # Multi-turn: build a conversation transcript
    parts: list[str] = []
    if system:
        parts.append(f"[System]: {system}\n")

    for i, msg in enumerate(non_system):
        role = msg.get("role", "user")
        text = _extract_text(msg.get("content", ""))
        is_last = i == len(non_system) - 1

        if role == "user":
            if is_last:
                parts.append(f"\n[Current message]:\n{text}")
            else:
                parts.append(f"User: {text}")
        elif role == "assistant":
            parts.append(f"Assistant: {text}")

    return "\n".join(parts)


class BusinessError(Exception):
    """DeepSeek refused the request while still answering HTTP 200.

    Captured live on 2026-08-19: with the anonymous account muted, the upstream
    replies 200 with a JSON envelope instead of an SSE stream --

        {"code":0,"msg":"","data":{"biz_code":5,"biz_msg":"user is muted",
                                   "biz_data":{"is_muted":1,"mute_until":1787189717.413}}}

    Nothing in that body starts with "data:", so the parser below simply found no
    fragments and the endpoint returned 200 with content "". A refusal reaching
    the caller as a successful empty answer is the worst possible shape for it:
    the gateway in front of this proxy could not distinguish a muted account from
    a model with nothing to say, so it kept sending traffic at an account that was
    being punished for precisely that.
    """

    def __init__(self, message: str, retry_after_epoch: float | None = None):
        super().__init__(message)
        # Absolute epoch seconds, as DeepSeek reports it (`mute_until`), not a
        # duration: converting it here would bake in the moment of parsing, and
        # the caller is what knows when it is answering.
        self.retry_after_epoch = retry_after_epoch


def _business_error(raw: str) -> BusinessError | None:
    """A BusinessError if this line is a refusal envelope, else None.

    Only a line that is NOT an SSE event is considered -- a real stream line
    starts with "data:" -- and only a NON-ZERO `biz_code` counts: the same
    envelope shape carries `biz_code: 0` when everything is fine.
    """
    try:
        payload = json.loads(raw)
    except Exception:
        return None                      # keepalives, blank lines, anything else
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("biz_code"):
        return None
    message = data.get("biz_msg") or f"biz_code {data.get('biz_code')}"
    biz_data = data.get("biz_data")
    until = biz_data.get("mute_until") if isinstance(biz_data, dict) else None
    return BusinessError(message, until if isinstance(until, (int, float)) else None)


def _retry_after_seconds(e: BusinessError) -> int | None:
    """DeepSeek's absolute `mute_until` as a duration from now, never negative.

    None when the refusal carried no deadline -- better no header at all than a
    made-up one, which the caller would honour just as literally.
    """
    if e.retry_after_epoch is None:
        return None
    return max(0, int(e.retry_after_epoch - time.time()))


def _retry_after_header(e: BusinessError) -> dict:
    seconds = _retry_after_seconds(e)
    return {} if seconds is None else {"Retry-After": str(seconds)}


def _iter_content_chunks(ds_resp):
    """
    Generator that yields plaintext content chunks from a DeepSeek streaming
    response, skipping <think> fragments.

    Raises BusinessError if the upstream refused the request in its body while
    answering HTTP 200 -- see that class for why that is not merely cosmetic.
    """
    thinking_done = False
    in_think = False

    for line in ds_resp.iter_lines():
        raw = line.decode() if isinstance(line, bytes) else line
        if not raw.startswith("data:"):
            refusal = _business_error(raw)
            if refusal is not None:
                raise refusal
            continue
        try:
            evt = json.loads(raw[5:])
        except Exception:
            continue

        path = evt.get("p", "")
        op   = evt.get("o", "")
        val  = evt.get("v", "")

        if path == "response/status" and val == "FINISHED":
            break

        if path == "response/fragments/-1/elapsed_secs" and op == "SET":
            thinking_done = True
            in_think = False
            continue

        if path == "response/fragments" and op == "APPEND":
            thinking_done = True
            frags = val if isinstance(val, list) else []
            for frag in frags:
                if not isinstance(frag, dict):
                    continue
                ftype   = frag.get("type", "")
                content = frag.get("content", "")
                if not content:
                    continue
                if ftype in ("THINK", "THINKING"):
                    in_think = True
                else:
                    in_think = False
                    yield content
            continue

        # `op` vacio cuenta como APPEND: es la MISMA optimizacion de diff que
        # los eventos sin ruta de mas abajo -- DeepSeek omite lo que se puede
        # deducir. Verificado contra el stream real (2026-08-17, instrumentando
        # el proxy en produccion) con deepseek-reasoner:
        #
        #   p='response/fragments'            o='APPEND'  -> fragmento con content='H'
        #   p='response/fragments/-1/content' o=''        -> v='ola'
        #
        # Exigir op=="APPEND" dejaba ese segundo evento sin rama: tiene `p`, asi
        # que tampoco entra en la continuacion sin ruta (`not path`), y se caia
        # por el hueco entre las dos. "Hola mundo hermoso" salia como "H mundo
        # hermoso" -- se perdia justo el primer trozo despues del pensamiento,
        # que es donde el reasoner abre el fragmento de contenido.
        #
        # NO se acepta cualquier `op`: un "SET" futuro significaria REEMPLAZAR
        # el contenido, no agregarlo, y tratarlo como append duplicaria texto.
        if path == "response/fragments/-1/content" and op in ("APPEND", ""):
            if not in_think:
                if val:
                    yield val
            continue

        # Continuacion sin ruta: DeepSeek manda el PRIMER trozo con `p` explicito
        # y los siguientes SIN `p` ni `o` — la ruta queda implicita (optimizacion
        # de diff). Verificado contra el stream real: "1, 2, 3, 4, 5" llega como
        # 1 evento con ruta + 17 sin ella. Ignorarlos truncaba la respuesta al
        # primer fragmento ("1," en vez de la lista entera).
        if not path and not op and isinstance(val, str):
            if not in_think and val:
                yield val
            continue

        # Full snapshot event
        if not path and isinstance(val, dict) and "response" in val:
            frags = val["response"].get("fragments", [])
            for frag in frags:
                ftype   = frag.get("type", "")
                content = frag.get("content", "")
                if content and ftype not in ("THINK", "THINKING"):
                    yield content
            # El snapshot ABRE el fragmento al que apuntan los APPEND siguientes,
            # asi que tiene que dejar `in_think` en el estado correcto. Sin esto,
            # deepseek-reasoner declaraba type='THINK' aca y despues filtraba todo
            # su razonamiento al contenido (" need answer in Spanish? User asks...").
            if frags:
                in_think = frags[-1].get("type", "") in ("THINK", "THINKING")


def _ds_completion(token: str, prompt: str, model_type: str, thinking: bool):
    """
    Create a DeepSeek session and POST /chat/completion.
    Returns (response, used_token) — used_token may differ if a 401 triggered a refresh.
    Retries once on 401 with a fresh token.
    """
    def _attempt(tk: str):
        auth_h     = _auth_headers(tk)
        session_id = _ds.create_session(auth_h)
        challenge  = _ds.get_pow_challenge(auth_h)
        nonce      = _ds.solve_pow(challenge)
        headers    = {**auth_h, "X-DS-PoW-Response": _ds.make_pow_header(challenge, nonce)}
        payload    = {
            "chat_session_id":   session_id,
            "parent_message_id": None,
            "prompt":            prompt,
            "ref_file_ids":      [],
            "search_enabled":    False,
            "thinking_enabled":  thinking,
            "model_type":        model_type,
            "stream":            True,
        }
        return _requests.post(
            _ds.BASE + "/api/v0/chat/completion",
            headers=headers, json=payload, stream=True, timeout=120,
        )

    resp = _attempt(token)
    if resp.status_code == 401:
        print("[completion] 401 — renovando token y reintentando...", file=sys.stderr)
        new_token = _tokens.refresh()
        resp = _attempt(new_token)

    return resp


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "service": "deepseek-openai-proxy"}


@app.get("/health")
def health():
    """The proxy capability contract (llm-libre spec 2026-08-20).

    Unauthenticated and vendor-call-free on purpose (spec 3.1): this is the
    gateway's health-sweep target and the container's own health check, and
    both must answer even when DeepSeek itself is unreachable. `capabilities`
    is EFFECTIVE -- what a request would achieve right now, not what this
    codebase implements -- so the gateway reads one boolean instead of
    learning what a DeepSeek session is. See capabilities.py.
    """
    state = capabilities.snapshot()
    return {
        "status":       "ok",
        "version":      APP_VERSION,
        "contract":     1,
        "provider":     "deepseek",
        "auth":         capabilities.auth_block(state),
        "capabilities": capabilities.effective(state),
    }


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": _MODELS}


class LoginRequest(BaseModel):
    email:    str
    password: str


@app.post("/v1/auth/login")
async def auth_login(body: LoginRequest):
    """
    Login with DeepSeek credentials and return the Bearer token.

    - Si el email coincide con el del .env: solo refresca el token (no toca EMAIL/PASSWORD).
    - Si el email es distinto: actualiza EMAIL y PASSWORD en .env con la nueva cuenta.
    - Siempre: guarda el token nuevo en DEEPSEEK_TOKEN (.env) y ~/.deepseek_token.
    """
    loop = asyncio.get_event_loop()
    try:
        token = await loop.run_in_executor(
            None, lambda: _ds.login(body.email, body.password)
        )
    except SystemExit as e:
        raise HTTPException(401, detail=str(e))
    except Exception as e:
        raise HTTPException(502, detail=str(e))

    # Overwrite stored credentials only when switching to a different account
    current_email, _ = _ds._env_creds()
    credentials_updated = False
    if body.email != current_email:
        await loop.run_in_executor(None, lambda: (
            _ds._write_env_key("EMAIL", body.email),
            _ds._write_env_key("PASSWORD", body.password),
        ))
        credentials_updated = True

    await loop.run_in_executor(None, lambda: _ds.save_token(token))
    _tokens.set(token)

    return {
        "token":               token,
        "token_type":          "Bearer",
        "credentials_updated": credentials_updated,
        "account":             body.email,
        "usage":               "Authorization: Bearer " + token,
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str = Header(default=None)):
    token   = _tokens.get(_bearer(authorization))
    body    = await request.json()

    model      = body.get("model", "deepseek-chat")
    messages   = body.get("messages") or []
    do_stream  = body.get("stream", False)

    if not messages:
        raise HTTPException(400, detail="messages is required")

    model_type = _MODEL_MAP.get(model, "default")
    thinking   = model_type == "expert"
    prompt     = _format_messages(messages)

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created       = int(time.time())

    loop = asyncio.get_event_loop()

    if do_stream:
        async def generate():
            try:
                ds_resp = await loop.run_in_executor(
                    None, lambda: _ds_completion(token, prompt, model_type, thinking)
                )
            except Exception as e:
                err = json.dumps({"error": {"message": str(e), "type": "api_error"}})
                yield f"data: {err}\n\n"
                return

            if ds_resp.status_code != 200:
                err = json.dumps({"error": {"message": f"DeepSeek {ds_resp.status_code}", "type": "api_error"}})
                yield f"data: {err}\n\n"
                return

            # Role chunk first
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(role_chunk)}\n\n"

            try:
                texts = await loop.run_in_executor(
                    None, lambda: list(_iter_content_chunks(ds_resp)))
            except BusinessError as e:
                # The 200 and the SSE headers went out before the body was read,
                # so there is no status left to change: the refusal has to reach
                # the client as an error EVENT. Silence here would look like a
                # perfectly ordinary empty answer.
                err = json.dumps({"error": {
                    "message": f"DeepSeek refused the request: {e}",
                    "type": "rate_limit_error",
                    "retry_after": _retry_after_seconds(e)}})
                yield f"data: {err}\n\n"
                return

            for text in texts:
                chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"

            stop_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(stop_chunk)}\n\ndata: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    else:
        def _run():
            ds_resp = _ds_completion(token, prompt, model_type, thinking)
            if ds_resp.status_code != 200:
                raise HTTPException(502, detail=f"DeepSeek error {ds_resp.status_code}")
            return "".join(_iter_content_chunks(ds_resp))

        try:
            content = await loop.run_in_executor(None, _run)
        except BusinessError as e:
            # 429, not 502, and with Retry-After: a mute is a rate limit, and this
            # is the one shape a client can act on precisely instead of guessing.
            # Returning 200-with-empty-content (what this did before) made the
            # caller keep hammering an account that was muted for exactly that.
            raise HTTPException(
                429, detail=f"DeepSeek refused the request: {e}",
                headers=_retry_after_header(e))

        return JSONResponse({
            "id":      completion_id,
            "object":  "chat.completion",
            "created": created,
            "model":   model,
            "choices": [{
                "index":         0,
                "message":       {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file:            UploadFile = File(...),
    model:           str        = Form(default="whisper-1"),
    language:        str        = Form(default=None),
    response_format: str        = Form(default="json"),
    authorization:   str        = Header(default=None),
):
    token  = _tokens.get(_bearer(authorization))
    auth_h = _auth_headers(token)

    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    data   = await file.read()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(data)
        tmp_path = tf.name

    try:
        loop = asyncio.get_event_loop()
        transcription, _audio_id = await loop.run_in_executor(
            None, lambda: _ds.asr_transcribe(auth_h, tmp_path)
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if response_format == "text":
        return transcription
    return JSONResponse({"text": transcription})


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
