"""Tests del historial local de conversaciones.

Nada acá toca la red ni a DeepSeek: todo lo que se prueba es el agrupamiento y
el almacenamiento, que es justamente la parte que no depende del vendor.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conversations  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Una base limpia por test.

    El módulo guarda una conexión única a nivel de módulo, así que hay que
    resetearla además de mover la ruta: si no, el primer test se lleva a los
    demás a su base.
    """
    monkeypatch.setattr(conversations, "DB_PATH", str(tmp_path / "conv.db"))
    monkeypatch.setattr(conversations, "_conn", None)
    monkeypatch.setattr(conversations, "_broken", None)
    return conversations


def turn(role, content):
    return {"role": role, "content": content}


# ── Agrupamiento: lo que convierte un log en una conversación ─────────────────

def test_a_first_message_has_no_conversation_to_continue(store):
    """`resolve` responde "ninguna todavía", no crea nada."""
    assert store.resolve([turn("user", "hola")]) is None


def test_the_conversation_is_born_when_the_answer_is_recorded(store):
    """Crear en `resolve` dejaría una conversación vacía por cada petición que
    DeepSeek rechaza, y los rechazos acá son frecuentes."""
    msgs = [turn("user", "hola")]
    assert store.list_page(10, 0) == ([], False)
    cid = store.record(None, msgs, "¡hola!")
    assert cid is not None
    assert len(store.list_page(10, 0)[0]) == 1


def test_a_refused_request_leaves_no_trace(store):
    """Se resuelve, DeepSeek rechaza, nunca se llama a `record`."""
    store.resolve([turn("user", "hola")])
    assert store.list_page(10, 0) == ([], False)


def test_a_follow_up_continues_the_same_conversation(store):
    first = [turn("user", "hola")]
    cid = store.record(store.resolve(first), first, "¡hola!")

    second = first + [turn("assistant", "¡hola!"), turn("user", "¿y ahora?")]
    assert store.resolve(second) == cid


def test_a_third_turn_still_continues(store):
    msgs = [turn("user", "uno")]
    cid = store.record(None, msgs, "R1")

    msgs = msgs + [turn("assistant", "R1"), turn("user", "dos")]
    assert store.resolve(msgs) == cid
    store.record(cid, msgs, "R2")

    msgs = msgs + [turn("assistant", "R2"), turn("user", "tres")]
    assert store.resolve(msgs) == cid


def test_an_unrelated_thread_gets_its_own_conversation(store):
    a = [turn("user", "hola")]
    cid_a = store.record(None, a, "¡hola!")
    b = [turn("user", "otra cosa")]
    assert store.record(store.resolve(b), b, "otra respuesta") != cid_a


def test_two_clients_asking_the_same_first_question_are_not_one_conversation(store):
    """Sin mensajes previos no hay hilo que continuar: cada uno abre el suyo."""
    msgs = [turn("user", "hola")]
    assert store.record(None, msgs, "R") != store.record(None, msgs, "R")


def test_whitespace_does_not_break_the_match(store):
    first = [turn("user", "hola")]
    cid = store.record(None, first, "¡hola!")
    noisy = [turn("user", "  hola  "), turn("assistant", "¡hola!"), turn("user", "seguí")]
    assert store.resolve(noisy) == cid


# ── Almacenamiento ────────────────────────────────────────────────────────────

def test_messages_are_stored_in_order(store):
    msgs = [turn("user", "hola")]
    cid = store.record(None, msgs, "¡hola!")
    assert [(m["role"], m["content"]) for m in store.messages_of(cid)] == [
        ("user", "hola"), ("assistant", "¡hola!")]


def test_resending_the_whole_thread_does_not_duplicate_it(store):
    """Un cliente OpenAI reenvía todo el historial en cada request."""
    msgs = [turn("user", "uno")]
    cid = store.record(None, msgs, "R1")

    msgs = msgs + [turn("assistant", "R1"), turn("user", "dos")]
    store.record(store.resolve(msgs), msgs, "R2")

    assert [(m["role"], m["content"]) for m in store.messages_of(cid)] == [
        ("user", "uno"), ("assistant", "R1"), ("user", "dos"), ("assistant", "R2")]


def test_the_title_comes_from_the_first_user_message(store):
    msgs = [turn("system", "sos útil"), turn("user", "qué es un tensor")]
    cid = store.record(None, msgs, "un arreglo n-dimensional")
    assert store.get(cid)["title"] == "qué es un tensor"


def test_image_parts_are_not_stored_as_text(store):
    """Este proxy nunca reenvía imágenes, así que guardarlas mentiría."""
    content = [{"type": "text", "text": "mirá"},
               {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]
    cid = store.record(None, [turn("user", content)], "ok")
    assert [m["content"] for m in store.messages_of(cid)] == ["mirá", "ok"]


# ── Listado ───────────────────────────────────────────────────────────────────

def test_listing_is_newest_first_and_paginates(store):
    ids = [store.record(None, [turn("user", f"pregunta {i}")], f"respuesta {i}")
           for i in range(5)]

    page1, more1 = store.list_page(2, 0)
    assert more1 is True and len(page1) == 2
    assert page1[0]["id"] == ids[-1]

    page2, _ = store.list_page(2, 2)
    assert {c["id"] for c in page1}.isdisjoint({c["id"] for c in page2})

    last, more_last = store.list_page(2, 4)
    assert more_last is False and len(last) == 1


def test_an_unknown_id_is_absent_not_empty(store):
    """404 y "conversación vacía" son respuestas distintas."""
    assert store.get("no-existe") is None
    assert store.messages_of("no-existe") is None


def test_a_recorded_conversation_is_never_empty(store):
    """Ya no existen conversaciones vacías: nacen con su primer turno."""
    cid = store.record(None, [turn("user", "hola")], "¡hola!")
    assert len(store.messages_of(cid)) == 2


# ── Sin base de datos ─────────────────────────────────────────────────────────

@pytest.fixture
def broken_store(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("soy un archivo, no un directorio")
    monkeypatch.setattr(conversations, "DB_PATH", str(blocker / "conv.db"))
    monkeypatch.setattr(conversations, "_conn", None)
    monkeypatch.setattr(conversations, "_broken", None)
    return conversations


def test_an_unusable_path_reports_unavailable(broken_store):
    assert broken_store.available() is False
    assert broken_store.unavailable_reason()


def test_without_a_database_nothing_raises(broken_store):
    """El historial nunca puede tumbar una respuesta de chat."""
    assert broken_store.resolve([turn("user", "hola")]) is None
    assert broken_store.record(None, [turn("user", "hola")], "¡hola!") is None
    assert broken_store.list_page(10, 0) == ([], False)
    assert broken_store.get("x") is None
    assert broken_store.messages_of("x") is None


# ── Cómo se ve desde el contrato ──────────────────────────────────────────────

def test_the_capability_follows_the_database_not_the_credentials(store):
    """`conversations` es el único booleano acá que no depende de la cuenta.

    Depende de si se puede escribir el historial, porque es historial LOCAL. Si
    alguien lo "arregla" para que siga el modo de sesión como los otros diez,
    este test lo caza.
    """
    import capabilities

    anon = capabilities.effective(capabilities.SessionState(mode="anonymous"))
    account = capabilities.effective(capabilities.SessionState(mode="account"))
    assert anon["conversations"] is True
    assert account["conversations"] is True


def test_without_a_database_the_contract_says_false(broken_store):
    """Un endpoint que responde y olvida todo en el próximo deploy es peor que
    uno que admite que no está disponible."""
    import capabilities
    assert capabilities.effective(
        capabilities.SessionState(mode="account"))["conversations"] is False


# ── El gate de §3.4 ───────────────────────────────────────────────────────────

def test_a_false_capability_answers_501_not_404(store):
    """404 es indistinguible de un error de ruteo, y 503 hace que el gateway
    reintente algo que nunca iba a funcionar en esta configuración."""
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)
    for method, path in (("post", "/v1/images/generations"),
                         ("post", "/v1/audio/speech"),
                         ("post", "/v1/translate"),
                         ("post", "/v1/files"),
                         ("get", "/v1/files"),
                         ("get", "/v1/files/abc"),
                         ("delete", "/v1/files/abc")):
        assert getattr(client, method)(path).status_code == 501, path


def test_the_gate_names_the_capability_and_where_to_look(store):
    from fastapi.testclient import TestClient
    import server

    detail = TestClient(server.app).post("/v1/translate").json()["detail"]
    assert "translate" in detail and "/health" in detail


def test_conversations_is_not_gated_off_when_history_works(store):
    """Es la única capacidad acá que sigue a la base y no a las credenciales."""
    import capabilities
    capabilities.require("conversations")   # no raise


def test_conversations_501s_when_history_cannot_be_stored(broken_store):
    import capabilities
    import pytest as _pytest
    from fastapi import HTTPException

    with _pytest.raises(HTTPException) as exc:
        capabilities.require("conversations")
    assert exc.value.status_code == 501
