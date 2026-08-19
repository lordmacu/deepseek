"""A business error from DeepSeek must not become an empty successful answer.

Captured live on 2026-08-19 by instrumenting the proxy in production: the
anonymous account had been muted, and DeepSeek reported it as

    HTTP 200
    {"code":0,"msg":"","data":{"biz_code":5,"biz_msg":"user is muted",
                               "biz_data":{"is_muted":1,"mute_until":1787189717.413}}}

That body is NOT an SSE stream -- no line starts with "data:" -- so the chunk
parser found nothing to yield and the endpoint answered 200 with content "".
Downstream (llm-libre) saw a successful, empty completion: it could not tell a
muted account from a model that simply had nothing to say, and kept sending
traffic at an account that was being punished for exactly that.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import BusinessError, _iter_content_chunks   # noqa: E402


class FakeResp:
    """Mimics the only thing _iter_content_chunks uses: iter_lines() in bytes."""
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        for line in self._lines:
            yield line.encode() if isinstance(line, str) else line


MUTED = json.dumps({"code": 0, "msg": "", "data": {
    "biz_code": 5, "biz_msg": "user is muted",
    "biz_data": {"is_muted": 1, "mute_until": 1787189717.413}}})


def test_a_muted_account_raises_instead_of_yielding_nothing():
    with pytest.raises(BusinessError) as e:
        list(_iter_content_chunks(FakeResp([MUTED])))
    assert "muted" in str(e.value)


def test_the_mute_deadline_is_carried_so_it_can_become_a_retry_after():
    with pytest.raises(BusinessError) as e:
        list(_iter_content_chunks(FakeResp([MUTED])))
    assert e.value.retry_after_epoch == pytest.approx(1787189717.413)


def test_a_business_error_without_a_deadline_still_raises():
    body = json.dumps({"code": 0, "data": {"biz_code": 9, "biz_msg": "rate limited"}})
    with pytest.raises(BusinessError) as e:
        list(_iter_content_chunks(FakeResp([body])))
    assert e.value.retry_after_epoch is None
    assert "rate limited" in str(e.value)


def test_a_healthy_stream_is_untouched():
    """The normal path must not notice any of this."""
    events = [{"p": "response/fragments", "o": "APPEND",
               "v": [{"type": "TEXT", "content": "Hola"}]},
              {"v": " mundo"}]
    lines = ["data:" + json.dumps(e) for e in events]
    assert "".join(_iter_content_chunks(FakeResp(lines))) == "Hola mundo"


def test_a_successful_envelope_is_not_mistaken_for_an_error():
    """biz_code 0 means everything is fine; only a non-zero one is an error."""
    ok = json.dumps({"code": 0, "data": {"biz_code": 0, "biz_msg": ""}})
    events = ["data:" + json.dumps({"v": "hola"})]
    assert "".join(_iter_content_chunks(FakeResp([ok] + events))) == "hola"


def test_noise_that_is_not_json_is_still_ignored():
    """Keepalives and blank lines must not be read as errors."""
    lines = ["", ": keepalive", "data:" + json.dumps({"v": "hola"})]
    assert "".join(_iter_content_chunks(FakeResp(lines))) == "hola"


# --- The HTTP surface ---------------------------------------------------------
#
# Detecting the refusal is only half of it: what the gateway in front of this
# proxy can DO about it depends entirely on the status code it receives. A 429
# with Retry-After is the one shape it already knows how to honour -- it cools the
# route down for exactly as long as the provider asked. A 502, or the 200 this
# used to return, both make it guess.

import server as srv                                          # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

CHAT = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hola"}]}


class _Resp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        for line in self._lines:
            yield line.encode()


@pytest.fixture
def muted(monkeypatch):
    monkeypatch.setattr(srv, "_ds_completion",
                        lambda *a, **k: _Resp([MUTED]))
    monkeypatch.setattr(srv._tokens, "get", lambda *a, **k: "tok")
    return TestClient(srv.app, raise_server_exceptions=False)


def test_a_muted_account_answers_429_not_200(muted):
    r = muted.post("/v1/chat/completions", json=CHAT)
    assert r.status_code == 429


def test_the_429_carries_retry_after_in_seconds(muted, monkeypatch):
    monkeypatch.setattr(srv.time, "time", lambda: 1787189717.413 - 3600)
    r = muted.post("/v1/chat/completions", json=CHAT)
    assert int(r.headers["Retry-After"]) == pytest.approx(3600, abs=2)


def test_the_message_says_what_actually_happened(muted):
    r = muted.post("/v1/chat/completions", json=CHAT)
    assert "muted" in json.dumps(r.json())


def test_a_deadline_already_past_does_not_produce_a_negative_retry_after(
        muted, monkeypatch):
    monkeypatch.setattr(srv.time, "time", lambda: 1787189717.413 + 500)
    r = muted.post("/v1/chat/completions", json=CHAT)
    assert int(r.headers["Retry-After"]) >= 0


def test_the_streaming_path_reports_the_refusal_too(muted):
    """It cannot change the status once the stream is open, so the refusal has to
    reach the client as an error event rather than as an empty, tidy stream."""
    r = muted.post("/v1/chat/completions", json={**CHAT, "stream": True})
    assert "muted" in r.text
