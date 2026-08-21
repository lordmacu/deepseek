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
credentials, no plan to resolve. So `snapshot()` mirrors grok-proxy's shape --
a single environment read, no cache, no lock, no vendor call -- rather than
chatgpt-proxy's refresh-interval AccountState cache, which exists only
because that account has paid plans worth polling.

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


def snapshot() -> SessionState:
    """Whether this process holds something that should let it authenticate.

    server.py's own notion of "authenticated" (`_TokenManager.get`, deepseek
    CLI module) is layered: a cached in-memory token, then a token loaded
    from `DEEPSEEK_TOKEN`/`.env`/`~/.deepseek_token` and validated with a live
    call to `/api/v0/users/current`, then auto-login with
    `DEEPSEEK_EMAIL`/`DEEPSEEK_PASSWORD` (`_ds.login`, another live call). The
    validating and login steps both reach the vendor, so none of that layer
    can run here -- `/health` must answer without a vendor call (spec 3.1).

    What CAN be read without one, and what this function reads, is exactly
    the three environment variables this repo's own docker-compose.yml
    injects into every container (see `environment:` there):
    `DEEPSEEK_EMAIL`, `DEEPSEEK_PASSWORD` (what `_auto_login` needs) and
    `DEEPSEEK_TOKEN` (a pre-obtained token that lets `_TokenManager` skip
    login entirely). "account" means this process holds one of those two
    credential shapes -- PRESENCE, not verified validity, the same
    evidentiary class as grok-proxy's `GROK_SESSION_TOKEN` check: a token
    that has since been revoked is only caught the day a real request to
    DeepSeek fails, not here.

    Deliberately NOT read: the mounted `.env` file's own EMAIL/PASSWORD/
    DEEPSEEK_TOKEN keys and `~/.deepseek_token` (the file-based fallbacks
    `_ds._env_creds`/`_ds.load_token` use when the matching env var is
    unset). In the actual deployment all three values are also exported as
    OS env vars by docker-compose, so this is not a gap there; it would only
    diverge from server.py's own resolution for a bare `python server.py`
    run with credentials placed exclusively in `.env` and never exported --
    a configuration this repo's own docker-compose.yml does not use.
    """
    email    = (os.environ.get("DEEPSEEK_EMAIL") or "").strip()
    password = (os.environ.get("DEEPSEEK_PASSWORD") or "").strip()
    token    = (os.environ.get("DEEPSEEK_TOKEN") or "").strip()
    has_credentials = bool(email and password) or bool(token)
    return SessionState(mode="account" if has_credentials else "anonymous")


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
        (server.py:514) and resolves a DeepSeek token via `_tokens.get`
        before doing anything else, so an unauthenticated process cannot
        make it succeed.
      `streaming` -- True when live. The same endpoint honours
        `stream: true` with a real SSE response, `data: [DONE]` terminated
        (server.py:535-589).
      `audio_transcription` -- True when live. `POST /v1/audio/transcriptions`
        exists (server.py:623), backed by `_ds.asr_transcribe` over
        `/api/v0/asr/ws` (the `deepseek` module), and resolves a token the
        same way `chat` does.

      `tools` -- False ALWAYS, live or not. This proxy has no function
        calling of its own: `_format_messages` (server.py:223-254) flattens
        the conversation into one prompt string, and nothing in
        `_iter_content_chunks` ever produces a `tool_calls` field. The
        GATEWAY emulates tool calling on top of this proxy's plain chat
        completions by prompt injection -- that is llm-libre's own
        `emulates_tools`, a capability of the gateway, not of this proxy.
        Reporting `tools: true` here would claim the gateway's work as
        deepseek's.
      `search` -- False ALWAYS, live or not. `_ds_completion` hardcodes
        `"search_enabled": False` in every completion payload it sends
        (server.py:437). The backend supports search; a request sent
        through THIS proxy right now never triggers it, credentials or not.
        The boolean reports what a request achieves, not what the vendor's
        backend is capable of (spec 3.2) -- so it stays False until
        server.py actually flips that field.
      `vision` -- False. `_extract_text` (server.py:211-220) reads only
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
      `conversations` -- False. `/api/v0/chat/history_messages` is real
        upstream, but server.py exposes no `/v1/conversations*` route.

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
