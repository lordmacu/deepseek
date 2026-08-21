"""The capability contract deepseek now publishes at GET /health.

Spec: llm-libre docs/superpowers/specs/2026-08-20-proxy-capability-contract-design.md

These tests protect the guarantees the contract is FOR:
  - every one of the eleven keys is present and boolean, always -- a missing
    key is what would let a stale hand-written providers.yaml fall back
    silently (spec 5.3), and a non-bool would break gateway parsing outright.
  - `tools` is false NO MATTER what credentials this process holds -- this
    proxy has no function calling of its own; the emulation lives in the
    gateway (llm-libre's `emulates_tools`), and reporting `tools: true` here
    would claim someone else's work (see capabilities.py's docstring).
  - `/health` answers with no Authorization header at all -- it is both the
    container health check and the gateway's unauthenticated sweep target
    (spec 3.1), and it must never depend on credentials being configured.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import capabilities  # noqa: E402

ANONYMOUS = capabilities.SessionState(mode="anonymous")
ACCOUNT   = capabilities.SessionState(mode="account")


def test_all_eleven_keys_are_present_and_boolean():
    caps = capabilities.effective(ACCOUNT)
    assert set(caps) == set(capabilities.REQUIRED_CAPABILITIES)
    assert len(caps) == 11
    assert all(isinstance(v, bool) for v in caps.values())


def test_the_anonymous_case_reports_false_for_every_credentialed_capability():
    caps = capabilities.effective(ANONYMOUS)
    assert caps["chat"] is False
    assert caps["streaming"] is False
    assert caps["audio_transcription"] is False


def test_tools_is_false_regardless_of_credentials():
    assert capabilities.effective(ANONYMOUS)["tools"] is False
    assert capabilities.effective(ACCOUNT)["tools"] is False


def test_search_is_false_regardless_of_credentials():
    """server.py:437 hardcodes search_enabled False in every completion
    payload -- a request sent right now never searches, live account or
    not."""
    assert capabilities.effective(ANONYMOUS)["search"] is False
    assert capabilities.effective(ACCOUNT)["search"] is False


def test_audio_transcription_is_true_with_credentials():
    assert capabilities.effective(ACCOUNT)["audio_transcription"] is True


def test_snapshot_reports_anonymous_with_no_credentials_configured(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_EMAIL", raising=False)
    monkeypatch.delenv("DEEPSEEK_PASSWORD", raising=False)
    monkeypatch.delenv("DEEPSEEK_TOKEN", raising=False)
    assert capabilities.snapshot().mode == "anonymous"


def test_snapshot_reports_account_with_email_and_password(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_EMAIL", "a@b.com")
    monkeypatch.setenv("DEEPSEEK_PASSWORD", "secret")
    monkeypatch.delenv("DEEPSEEK_TOKEN", raising=False)
    assert capabilities.snapshot().mode == "account"


def test_snapshot_reports_account_with_a_cached_token_alone(monkeypatch):
    """DEEPSEEK_TOKEN lets _TokenManager skip login entirely -- it is its own
    sufficient credential, no email/password needed."""
    monkeypatch.delenv("DEEPSEEK_EMAIL", raising=False)
    monkeypatch.delenv("DEEPSEEK_PASSWORD", raising=False)
    monkeypatch.setenv("DEEPSEEK_TOKEN", "some-cached-token")
    assert capabilities.snapshot().mode == "account"


def test_snapshot_reports_anonymous_with_only_half_a_credential_pair(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_EMAIL", "a@b.com")
    monkeypatch.delenv("DEEPSEEK_PASSWORD", raising=False)
    monkeypatch.delenv("DEEPSEEK_TOKEN", raising=False)
    assert capabilities.snapshot().mode == "anonymous"


def test_auth_block_never_invents_a_plan():
    block = capabilities.auth_block(ACCOUNT)
    assert block == {"mode": "account", "plan": None,
                      "subscription_active": False, "expires_at": None}


# --- /health -------------------------------------------------------------

import server as srv                                             # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402


def test_health_answers_without_an_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_EMAIL", raising=False)
    monkeypatch.delenv("DEEPSEEK_PASSWORD", raising=False)
    monkeypatch.delenv("DEEPSEEK_TOKEN", raising=False)
    client = TestClient(srv.app)
    r = client.get("/health")
    assert r.status_code == 200


def test_health_publishes_the_full_contract_shape(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_EMAIL", raising=False)
    monkeypatch.delenv("DEEPSEEK_PASSWORD", raising=False)
    monkeypatch.delenv("DEEPSEEK_TOKEN", raising=False)
    client = TestClient(srv.app)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["provider"] == "deepseek"
    assert body["contract"] == 1
    assert "version" in body
    assert set(body["capabilities"]) == set(capabilities.REQUIRED_CAPABILITIES)
    assert all(isinstance(v, bool) for v in body["capabilities"].values())
    assert body["auth"]["mode"] == "anonymous"
    assert body["capabilities"]["tools"] is False


def test_health_reports_account_mode_when_credentials_are_configured(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_EMAIL", "a@b.com")
    monkeypatch.setenv("DEEPSEEK_PASSWORD", "secret")
    client = TestClient(srv.app)
    body = client.get("/health").json()
    assert body["auth"]["mode"] == "account"
    assert body["capabilities"]["chat"] is True
    assert body["capabilities"]["tools"] is False


# --- Dockerfile manifest --------------------------------------------------
#
# A module missing from the Dockerfile's COPY line produces a container that
# crash-loops on ModuleNotFoundError with the port dark -- this has happened
# twice in this codebase family, once in production.

def test_dockerfile_copies_the_capabilities_module():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "Dockerfile")) as f:
        copy_lines = [l for l in f if l.strip().startswith("COPY")]
    assert any("capabilities.py" in l for l in copy_lines)
