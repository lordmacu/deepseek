"""Conversation history for deepseek-proxy, kept locally in SQLite.

WHY LOCALLY, AND WHY THAT IS NOT A WORKAROUND: DeepSeek's backend does not
store a conversation list. Its Android client keeps one in a device-local
database -- `database.f("chat_session_list", ...)` in
decompiled_jadx/sources/defpackage/v6a.java, backed by libWCDB -- and the full
upstream API surface (37 paths, enumerated from the decompiled client) has no
endpoint that enumerates anything. `chat/history_messages` replays ONE session
whose id you already hold; `chat_session/create` and `chat_session/delete_all`
are the only other session routes.

So this module makes the proxy the device. That is the same architecture the
official client uses, not a substitute for a server feature that exists.

WHAT A CONVERSATION IS HERE. Deliberately NOT one of DeepSeek's sessions.
`server.py` creates a throwaway `chat_session` per request and always sends
`parent_message_id: None`, so DeepSeek's own sessions are one exchange each --
indexing them would produce thousands of one-message "conversations". Instead a
conversation is the CLIENT's thread, reconstructed the way perplexity-proxy
does it: an incoming request whose earlier messages match a stored tail is a
continuation of that conversation, and anything else starts a new one. That
grouping is what makes the listing worth reading.

SCOPE, which the capability boolean depends on: this lists conversations that
went THROUGH this proxy. Whatever was said in DeepSeek's own app or website is
invisible here, because it never touched this process -- exactly the same limit
the Android client has on a second device.

DURABILITY: the database is a file. Without a persistent volume mounted at its
directory, a redeploy starts an empty history. `available()` reports whether
the file is usable at all, and `capabilities.py` reads that -- so a deployment
with no writable path reports `conversations: false` instead of serving an
endpoint that silently forgets everything.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

DB_PATH = os.getenv("DEEPSEEK_DB_PATH", "/app/data/conversations.db")
TITLE_MAX = 80

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_broken: Optional[str] = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    tail_key   TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL,
    position        INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (conversation_id, position)
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_tail ON conversations(tail_key);
"""


def _connect() -> Optional[sqlite3.Connection]:
    """Open the database once, or remember why it could not be opened.

    A failure here is never fatal to the process: a proxy that cannot write
    history must still answer chat requests. The reason is kept so `/health`
    can report `conversations: false` honestly instead of leaving the operator
    to discover an empty list later.
    """
    global _conn, _broken
    if _conn is not None or _broken is not None:
        return _conn
    try:
        directory = os.path.dirname(DB_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.executescript(_SCHEMA)
        conn.commit()
        _conn = conn
    except Exception as exc:
        _broken = f"{type(exc).__name__}: {exc}"
    return _conn


def available() -> bool:
    """Whether history can actually be stored and served right now."""
    with _lock:
        return _connect() is not None


def unavailable_reason() -> Optional[str]:
    with _lock:
        _connect()
        return _broken


def _text(content: Any) -> str:
    """Flatten an OpenAI `content` into text.

    Mirrors `server.py::_extract_text`: only `type == "text"` parts survive,
    because that is all this proxy ever forwards upstream. Storing an
    `image_url` part would record a message the model never saw.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return ""


def _key(messages: list) -> str:
    """A stable fingerprint of a message sequence.

    Same idea as perplexity-proxy's `_cache_key`: role plus stripped text, so
    the fingerprint survives whitespace differences between a client's echo of
    a turn and what was stored, but changes the moment the content does.
    """
    payload = json.dumps(
        [{"r": m.get("role", ""), "c": _text(m.get("content")).strip()} for m in messages],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _title_from(messages: list) -> str:
    for m in messages:
        if m.get("role") == "user":
            text = " ".join(_text(m.get("content")).split())
            if text:
                return text[:TITLE_MAX]
    return "(sin título)"


def resolve(messages: list) -> Optional[str]:
    """Find the conversation this request continues, or start a new one.

    The lookup is on the messages BEFORE the new one: a request carrying
    [Q1, A1, Q2] continues the conversation whose stored tail is [Q1, A1].
    A first turn has no prior messages and therefore always opens a new
    conversation, which is correct -- two clients independently asking the same
    first question are not in the same conversation.
    """
    if not messages:
        return None
    with _lock:
        conn = _connect()
        if conn is None:
            return None

        prior = messages[:-1]
        if prior:
            row = conn.execute(
                "SELECT id FROM conversations WHERE tail_key = ? ORDER BY updated_at DESC LIMIT 1",
                (_key(prior),),
            ).fetchone()
            if row:
                return row[0]

        now = time.time()
        conversation_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO conversations (id, title, tail_key, created_at, updated_at)"
            " VALUES (?, ?, NULL, ?, ?)",
            (conversation_id, _title_from(messages), now, now),
        )
        conn.commit()
        return conversation_id


def record(conversation_id: Optional[str], messages: list, answer: str) -> None:
    """Store the turn that just completed and move the conversation's tail.

    Written to be unable to break a chat response: every failure is swallowed.
    A lost history entry is a smaller harm than a 500 on an answer the user
    already received, and the caller runs this AFTER the upstream call has
    succeeded.
    """
    if not conversation_id or not messages:
        return
    try:
        with _lock:
            conn = _connect()
            if conn is None:
                return
            now = time.time()
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            position = (row[0] if row else -1) + 1

            # Only the turns this proxy has not stored yet: a client that
            # re-sends the whole thread every request would otherwise duplicate
            # every earlier message on every call.
            for message in messages[position:]:
                text = _text(message.get("content"))
                if not text:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO messages"
                    " (conversation_id, position, role, content, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (conversation_id, position, message.get("role", "user"), text, now),
                )
                position += 1

            if answer:
                conn.execute(
                    "INSERT OR REPLACE INTO messages"
                    " (conversation_id, position, role, content, created_at)"
                    " VALUES (?, ?, 'assistant', ?, ?)",
                    (conversation_id, position, answer, now),
                )

            tail = list(messages) + ([{"role": "assistant", "content": answer}] if answer else [])
            conn.execute(
                "UPDATE conversations SET tail_key = ?, updated_at = ? WHERE id = ?",
                (_key(tail), now, conversation_id),
            )
            conn.commit()
    except Exception:
        return


def _iso(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _row_to_item(row: sqlite3.Row | tuple) -> dict:
    return {
        "id": row[0],
        "title": row[1] or None,
        "generated_title": None,
        "updated_at": _iso(row[2]),
        "pinned": False,
        "project_id": None,
    }


def list_page(limit: int, offset: int) -> tuple[list[dict], bool]:
    """One page of conversations, newest first, plus whether more remain."""
    with _lock:
        conn = _connect()
        if conn is None:
            return [], False
        rows = conn.execute(
            "SELECT id, title, updated_at FROM conversations"
            " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit + 1, offset),
        ).fetchall()
    return [_row_to_item(r) for r in rows[:limit]], len(rows) > limit


def get(conversation_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        if conn is None:
            return None
        row = conn.execute(
            "SELECT id, title, updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    return _row_to_item(row) if row else None


def messages_of(conversation_id: str) -> Optional[list[dict]]:
    """Every stored turn, or None when the conversation does not exist.

    None and [] are different answers on purpose: an unknown id is a 404, while
    a known conversation with nothing stored yet is an empty list.
    """
    with _lock:
        conn = _connect()
        if conn is None:
            return None
        exists = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not exists:
            return None
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ?"
            " ORDER BY position ASC",
            (conversation_id,),
        ).fetchall()
    return [{"role": r[0], "content": r[1], "id": None} for r in rows]
