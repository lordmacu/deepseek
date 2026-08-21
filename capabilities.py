"""What this proxy can actually do right now.

Spec: the proxy capability contract, llm-libre
docs/superpowers/specs/2026-08-20-proxy-capability-contract-design.md

THE RULE: a boolean says what a request sent right now would ACHIEVE, not what
this codebase implements. Where the two differ, the endpoint is the liar and
this module is the correction (spec 3.2).

Where the rule STOPS: a boolean tracks entitlement, not the meter. A quota
running out is a 429 the gateway already handles with a cooldown and recovers
from on its own; it must never flip a capability off. The dividing line is
durability -- if a fresh request tomorrow would still be refused for the same
reason, it belongs in the boolean.

Unlike chatgpt-proxy, deepseek sells no tiers: one account, one set of
credentials, no plan to resolve. So `snapshot()` follows grok-proxy's shape in
spirit -- no lock, no vendor call, no refresh-interval cache like
chatgpt-proxy's AccountState, which exists only because that account has paid
plans worth polling. It checks more local places than grok does, though:
`DEEPSEEK_EMAIL`/`DEEPSEEK_PASSWORD`, plus -- via `_ds.load_token()`, the same
function server.py itself uses -- three more: the `DEEPSEEK_TOKEN` env var,
`.env`'s own `DEEPSEEK_TOKEN` key, and `~/.deepseek_token`. Grok's equivalent
checks exactly one env var and has no persistent file cache to fall behind
it; deepseek does, because credentials here can be refreshed on disk while
the container keeps running (see `snapshot()`'s docstring for why that
distinction is load-bearing, not decorative). All of it stays a local read,
never a vendor call.

IMPORTANT, and different from grok/chatgpt: nothing in this module was
exercised against a live DeepSeek backend as part of writing it. Every value
in `effective()` below is READ OFF server.py and the `deepseek` CLI module it
loads -- static inspection, not a measured pass rate. The docstring on
`effective()` says so per boolean; do not mistake "the code path clearly does
X" for "a live call confirmed X".
"""
import os
from dataclasses import dataclass

REQUIRED_CAPABILITIES = (
    "chat", "streaming", "tools", "vision", "images",
    "audio_speech", "audio_transcription", "translate",
    "search", "files", "conversations",
)


@dataclass(frozen=True)
class SessionState:
    mode: str          # "account" | "anonymous"


# ── Reusing the `deepseek` CLI module's own token resolution ─────────────────
# server.py loads this same file the same way (SourceFileLoader, because it has
# no .py extension) to get its `_ds`; duplicated here rather than imported from
# server.py to avoid a circular import (server.py imports capabilities, not the
# other way round). The only thing this module borrows from it is
# `load_token()` -- a pure local read (env var -> `.env` file ->
# `~/.deepseek_token`, no network -- see that function's own body) -- so
# `snapshot()` can see a persisted token the same way server.py would, without
# re-deriving "~/.deepseek_token" by hand and risking it drifting from the real
# constant (`deepseek`'s own `TOKEN_FILE`).
import importlib.machinery
import importlib.util

_CLI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepseek")
_loader   = importlib.machinery.SourceFileLoader("ds_capabilities", _CLI_PATH)
_spec     = importlib.util.spec_from_loader("ds_capabilities", _loader)
_ds       = importlib.util.module_from_spec(_spec)
_loader.exec_module(_ds)


def snapshot() -> SessionState:
    """Whether this process holds something that should let it authenticate.

    server.py's own notion of "authenticated" (`_TokenManager.get`, deepseek
    CLI module) is layered: a cached in-memory token, then a token loaded via
    `_ds.load_token()` and validated with a live call to
    `/api/v0/users/current`, then auto-login with `DEEPSEEK_EMAIL`/
    `DEEPSEEK_PASSWORD` (`_ds.login`, another live call). The validating and
    login steps both reach the vendor, so neither can run here -- `/health`
    must answer without a vendor call (spec 3.1).

    What CAN be read without one is credential PRESENCE, not verified
    validity -- the same evidentiary class as grok-proxy's
    `GROK_SESSION_TOKEN` check: a token that has since been revoked is only
    caught the day a real request to DeepSeek fails, not here. Two sources
    count as "account":

      1. `DEEPSEEK_EMAIL` + `DEEPSEEK_PASSWORD` env vars -- what
         `_auto_login` needs, read directly. These match two of the three
         vars this repo's own docker-compose.yml injects into every
         container.
      2. `_ds.load_token()` returning a non-empty token -- reused rather
         than re-derived. This is the source grok's equivalent check never
         needed: grok has exactly one token in exactly one env var and no
         persistent cache to fall behind it. deepseek is different --
         docker-compose.yml mounts `~/.deepseek_token` into the container
         ("Token file como caché adicional"), and both `_TokenManager`'s
         in-memory refresh and the `deepseek` CLI's `save_token()` write a
         freshly-renewed token to that file WHILE THE CONTAINER RUNS, never
         into `os.environ` (fixed at container start). So a redeploy that
         rotates credentials out of the environment but keeps that mounted
         file would leave `os.environ` empty while the proxy keeps serving
         requests off the cached token -- checking only env vars would
         report `anonymous` for a proxy that is demonstrably `account`,
         which is the contract lying in the unsafe direction: the gateway
         would route away from a provider that works. `_ds.load_token()`
         itself is a plain local read (env var, then the `.env` file's
         `DEEPSEEK_TOKEN` key, then a stat-and-read of
         `~/.deepseek_token`) -- no network, so it fits `/health`'s
         constraint exactly. Only presence is checked here; the token's
         contents are never inspected or validated -- that stays server.py's
         job, the day a real request either succeeds or gets a 401.
    """
    email    = (os.environ.get("DEEPSEEK_EMAIL") or "").strip()
    password = (os.environ.get("DEEPSEEK_PASSWORD") or "").strip()
    has_env_credentials = bool(email and password)
    has_cached_token    = bool(_ds.load_token())
    mode = "account" if (has_env_credentials or has_cached_token) else "anonymous"
    return SessionState(mode=mode)


def auth_block(state: SessionState) -> dict:
    """The contract's informational `auth` block.

    Every field except `mode` is null on purpose: deepseek sells no tiers, so
    there is no plan to name and no subscription to expire. Reporting a
    placeholder here would be the same class of lie the contract exists to
    end.
    """
    return {"mode": state.mode, "plan": None,
            "subscription_active": False, "expires_at": None}


def effective(state: SessionState) -> dict:
    """The eleven booleans this proxy can honestly claim, and why.

    Every value below comes from READING server.py and the `deepseek` CLI
    module it loads at import time -- none of it is a live measurement taken
    while writing this module. Where an endpoint exists and nothing in its
    code path refuses regardless of credentials, the boolean follows `live`
    (this process holds credentials -- see `snapshot()`). Where the code path
    itself refuses unconditionally, the boolean is False no matter what
    `live` is -- crediting `live` there would be exactly the overclaim the
    contract exists to prevent.

      `chat` -- True when live. `POST /v1/chat/completions` exists
        (server.py:541) and resolves a DeepSeek token via `_tokens.get`
        before doing anything else, so an unauthenticated process cannot
        make it succeed.
      `streaming` -- True when live. The same endpoint honours
        `stream: true` with a real SSE response, `data: [DONE]` terminated
        (server.py:561-615).
      `audio_transcription` -- True when live. `POST /v1/audio/transcriptions`
        exists (server.py:650), backed by `_ds.asr_transcribe` over
        `/api/v0/asr/ws` (the `deepseek` module), and resolves a token the
        same way `chat` does.

      `tools` -- False ALWAYS, live or not. This proxy has no function
        calling of its own: `_format_messages` (server.py:227-258) flattens
        the conversation into one prompt string, and nothing in
        `_iter_content_chunks` ever produces a `tool_calls` field. The
        GATEWAY emulates tool calling on top of this proxy's plain chat
        completions by prompt injection -- that is llm-libre's own
        `emulates_tools`, a capability of the gateway, not of this proxy.
        Reporting `tools: true` here would claim the gateway's work as
        deepseek's.
      `search` -- False ALWAYS, live or not. `_ds_completion` hardcodes
        `"search_enabled": False` in every completion payload it sends
        (server.py:441). The backend supports search; a request sent
        through THIS proxy right now never triggers it, credentials or not.
        The boolean reports what a request achieves, not what the vendor's
        backend is capable of (spec 3.2) -- so it stays False until
        server.py actually flips that field.
      `vision` -- False. `_extract_text` (server.py:215-224) reads only
        content parts with `type == "text"`; an `image_url` part is
        silently dropped before it ever reaches the model.
      `images` -- False. No `/v1/images/generations` route exists.
      `audio_speech` -- False. No `/v1/audio/speech` route exists, and no
        text-to-speech call appears anywhere in server.py or the `deepseek`
        module.
      `translate` -- False. No `/v1/translate` route exists.
      `files` -- False. `deepseek:249 upload_file()` reaches
        `/api/v0/file/upload_file` upstream, but server.py never calls it
        and exposes no `/v1/files*` route -- the capability exists in the
        vendor, not in this proxy.
      `conversations` -- False. server.py exposes no `/v1/conversations*`
        route. `/api/v0/chat/history_messages` is the endpoint named for
        this capability in this task's own brief; it was NOT independently
        confirmed against the `deepseek` CLI module or the decompiled APK in
        this repo (unlike `files`, whose `/api/v0/file/upload_file` is real
        and callable at `deepseek:249 upload_file()`) -- so treat that
        specific path as reported, not verified, until someone checks it
        against a live account or the client's own protocol.

    None of the seven unconditional Falses above can be made True by an
    account gaining credentials -- each needs new code (spec 8, "what comes
    after"), which is deliberately out of scope here: the contract's value is
    reporting the current truth precisely, not making more of it true.
    """
    live = state.mode == "account"
    return {
        "chat":                live,
        "streaming":           live,
        "tools":               False,
        "vision":              False,
        "images":              False,
        "audio_speech":        False,
        "audio_transcription": live,
        "translate":           False,
        "search":              False,
        "files":               False,
        "conversations":       False,
    }
