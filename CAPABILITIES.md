# Capacidades de deepseek (proxy)

A diferencia de chatgpt-proxy, deepseek no vende planes ni niveles de cuenta:
hay una sola cuenta detrás de este despliegue, así que no hay una matriz
anónima/free/plan que comparar — solo dos estados, **con credenciales** y
**sin credenciales** — igual que grok-proxy.

Pero a diferencia de grok-proxy y chatgpt-proxy, **nada de lo que sigue surge
de una medición en vivo contra el backend real de DeepSeek**. chatgpt-proxy
midió con `smoke_test.py`/`compare_accounts.py`; grok-proxy midió contra su
pool de modelos (30/31 rutas leyendo un código de una imagen, etc.). Esta
tabla se construyó **leyendo `server.py` y el módulo `deepseek` que carga**,
sin ejecutar un solo request real — auditoría estática, no una corrida
contra la cuenta. Se dice así de plano, sección por sección, en vez de
tomar prestada la confianza de los otros dos documentos: no hay pass rate
que citar acá porque no se corrió nada.

## Cómo se resuelve el estado de sesión (anónimo / cuenta)

`capabilities.snapshot()` (en `capabilities.py`) decide `"account"` cuando
cualquiera de dos fuentes **locales** está presente — nunca llama al vendor,
tal como exige la spec §3.1 para `/health`:

1. Las variables de entorno `DEEPSEEK_EMAIL` + `DEEPSEEK_PASSWORD` (lo que
   `_auto_login` necesita), leídas directo de `os.environ`.
2. `_ds.load_token()` — la misma función que usa `_TokenManager` —
   devolviendo un token no vacío. Esto cubre tanto la variable de entorno
   `DEEPSEEK_TOKEN` como el archivo `~/.deepseek_token` que
   `docker-compose.yml` monta como caché persistente ("Token file como
   caché adicional"). Es importante: ese archivo puede tener un token
   **fresco** que `_TokenManager`/`save_token()` guardaron mientras el
   contenedor corría, sin que eso toque nunca `os.environ` (fijo al
   arrancar el contenedor). Si `snapshot()` solo mirara variables de
   entorno, un redeploy que rotara las credenciales fuera del entorno pero
   conservara el archivo montado reportaría `anonymous` para un proxy que
   en los hechos sigue sirviendo `chat`/`streaming`/`audio_transcription`
   sin problema — el contrato mintiendo en la dirección insegura (el
   gateway dejaría de rutear a un proveedor que funciona). Por eso se
   revisan ambas fuentes.

Ninguna de las dos comprueba que la credencial sea **válida** — solo que
está **presente**, la misma clase de evidencia que el chequeo de
`GROK_SESSION_TOKEN` en grok-proxy. Un token revocado solo se descubre el
día que un request real a DeepSeek falla; eso lo sigue manejando
`server.py` (`_TokenManager`, con reintento y re-login reactivo en el
401), no `capabilities.py`.

## Matriz de capacidades

| Capacidad | Valor | Endpoint / RPC | Cómo se estableció el valor |
|---|:---:|---|---|
| `chat` | ✅ (con credenciales) | `POST /v1/chat/completions` (`server.py:541`) | Leído del código: el handler existe y resuelve un token vía `_tokens.get` antes de hacer cualquier otra cosa, así que un proceso sin credenciales no puede completarlo. No medido en vivo. |
| `streaming` | ✅ (con credenciales) | Mismo endpoint, `stream: true` → SSE, terminado en `data: [DONE]` (`server.py:561-615`) | Leído del código. No medido en vivo. |
| `audio_transcription` | ✅ (con credenciales) | `POST /v1/audio/transcriptions` (`server.py:650`), respaldado por `_ds.asr_transcribe` sobre `/api/v0/asr/ws` | Leído del código. No medido en vivo. |
| `tools` | ❌ (siempre) | — | Este proxy no tiene function calling propio: `_format_messages` (`server.py:227-258`) aplana la conversación en un string de prompt, y nada en `_iter_content_chunks` produce `tool_calls`. La emulación por inyección de prompt vive en el **gateway** (`emulates_tools` de llm-libre) — es trabajo del gateway, no de este proxy, y reportar `true` acá se lo atribuiría. |
| `search` | ❌ (siempre) | — | `_ds_completion` (`server.py:441`) manda `"search_enabled": False` en cada payload de completion, sin condición. El backend de DeepSeek sí soporta búsqueda, pero un request enviado por **este proxy**, ahora mismo, nunca la dispara — el booleano describe lo que un request logra, no lo que el backend podría hacer (spec §3.2). |
| `vision` | ❌ | — | `_extract_text` (`server.py:215-224`) solo lee partes de contenido con `type == "text"`; una parte `image_url` se descarta en silencio antes de llegar al modelo. |
| `images` | ❌ | — | No existe la ruta `POST /v1/images/generations`. |
| `audio_speech` | ❌ | — | No existe la ruta `POST /v1/audio/speech`, y no hay ninguna llamada de texto-a-voz en `server.py` ni en el módulo `deepseek`. |
| `translate` | ❌ | — | No existe la ruta `POST /v1/translate`. |
| `files` | ❌ | `/api/v0/file/upload_file` (upstream — ver `deepseek:249 upload_file()`) | La RPC existe y funciona contra DeepSeek, pero `server.py` nunca la llama y no expone ninguna ruta `/v1/files*`. La capacidad existe en el vendor, no en este proxy. Se nombra acá el endpoint upstream a propósito, para que quien la exponga después no tenga que redescubrirlo. |
| `conversations` | ❌ | `/api/v0/chat/history_messages` (upstream) | Igual que `files`: el endpoint upstream es real (nombrado en la auditoría de la spec, §1), pero `server.py` no expone ninguna ruta `/v1/conversations*`. Nombrado acá por la misma razón. |

Sin credenciales — ni variables de entorno ni token cacheado en
`~/.deepseek_token` — las tres capacidades marcadas "✅ (con credenciales)"
caen a `false`: los tres endpoints resuelven un token de DeepSeek antes de
hacer cualquier otra cosa. `tools` y `search` quedan en `false` sin
importar el estado de credenciales — no son un problema de sesión, son
código que nunca los activa, con o sin cuenta.

## Por qué esta tabla dice "leído del código" y no "medido"

chatgpt-proxy y grok-proxy construyeron su matriz corriendo herramientas
contra una cuenta real o midiendo pass rates contra su pool de modelos.
Este documento no: se construyó auditando `server.py` y el módulo
`deepseek` línea por línea, sin ejecutar un solo request real contra el
backend de DeepSeek. Eso es evidencia más débil — un endpoint que "en el
código se ve bien" puede fallar en la práctica por una razón que la
lectura estática no detecta — y decirlo es parte de lo que este contrato
exige: no reclamar una medición que no se hizo. Portar `smoke_test.py` a
este proxy (spec §6: *"that is smoke_test.py's job, which already exists
in chatgpt-proxy and gets ported to the other four"*) sigue pendiente.

## El gate `501` — todavía no implementado acá

La spec (§3.4) exige que un endpoint cuya capacidad es `false` responda
**`501 Not Implemented`**, no `404`, porque un `404` es indistinguible de
un error de ruteo. grok-proxy y chatgpt-proxy ya montan ese gate delante
de sus rutas gated. **deepseek todavía no**: `POST /v1/images/generations`,
`POST /v1/audio/speech`, `POST /v1/translate` y las rutas
`/v1/files*`/`/v1/conversations*` simplemente no existen como rutas en
`server.py`, así que FastAPI responde su `404` genérico, no un `501` a
propósito. El booleano en `GET /health` ya es correcto (`false` en los
cinco), pero el código de estado que vería un cliente real al intentarlo
todavía no cumple la letra de §3.4. Cerrar esto — añadir los cinco
handlers gated, sin implementar la funcionalidad real detrás — es trabajo
aparte, deliberadamente fuera del alcance de esta tarea: publicar el
contrato no incluía exponer ni cerrar superficie nueva.

## `GET /health`

Publica el contrato completo bajo `capabilities`, más `status`, `version`,
`contract: 1`, `provider: "deepseek"` y `auth` (solo `mode` varía —
`plan`, `subscription_active` y `expires_at` son siempre `null`/`false`:
deepseek no vende niveles, así que inventar un valor ahí sería la misma
clase de mentira que el contrato existe para terminar). Es la referencia
que **no se desactualiza**; esta tabla es la referencia humana, y sí puede
quedar atrás si el código cambia y nadie actualiza el markdown.
