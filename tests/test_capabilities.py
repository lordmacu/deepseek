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
  - a persisted token file counts as "account" on its own -- docker-compose.yml
    mounts ~/.deepseek_token as a cache that can outlive the environment a
    redeploy set up, and env-vars-only would report `anonymous` for a proxy
    that is demonstrably still serving requests.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import capabilities  # noqa: E402

ANONYMOUS = capabilities.SessionState(mode="anonymous")
ACCOUNT   = capabilities.SessionState(mode="account")


@pytest.fixture
def no_local_credentials(monkeypatch, tmp_path):
    """Neutralizes every local source snapshot() reads.

    snapshot() checks DEEPSEEK_EMAIL/PASSWORD directly, plus whatever
    _ds.load_token() (deepseek:85-96) finds. That function resolves, IN
    ORDER, exactly three places a token can be cached locally:

        1. the DEEPSEEK_TOKEN env var                    (delenv, below)
        2. .env's own DEEPSEEK_TOKEN key, via _ds.ENV_FILE   (repointed, below)
        3. ~/.deepseek_token, via _ds.TOKEN_FILE             (repointed, below)

    All three have to be neutralized, not just the first and third: this
    repo's own .env carries a DEEPSEEK_TOKEN key that happens to be empty
    right now, but save_token() (deepseek:99-104) writes a fresh one there on
    every real login or token refresh -- so the next time someone runs the
    CLI or the proxy re-authenticates on this machine, source 2 stops being
    empty, and any "anonymous" assertion that only cleared 1 and 3 would
    start silently reporting "account" instead. (This was caught in review:
    an earlier version of this fixture repointed TOKEN_FILE but not
    ENV_FILE, and every "anonymous" test here passed only because that .env
    key happened to be blank.)

    If `_ds.load_token()` ever grows a fourth local source, it has to be
    added to the list above AND neutralized below, or it slips through
    exactly the same way source 2 did.

    Returns the (not-yet-existing) TOKEN_FILE path so a test that wants "a
    cached token in the file alone" can just write to it; `capabilities._ds`
    is available directly for a test that wants the ENV_FILE path instead
    (see test_snapshot_reports_account_from_a_dot_env_token_alone below).
    """
    monkeypatch.delenv("DEEPSEEK_EMAIL", raising=False)
    monkeypatch.delenv("DEEPSEEK_PASSWORD", raising=False)
    monkeypatch.delenv("DEEPSEEK_TOKEN", raising=False)                 # source 1
    monkeypatch.setattr(capabilities._ds, "ENV_FILE",                   # source 2
                         str(tmp_path / "no-such-.env"))
    token_file = tmp_path / "no-such-token-file"                        # source 3
    monkeypatch.setattr(capabilities._ds, "TOKEN_FILE", str(token_file))
    return token_file


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
    """server.py:441 hardcodes search_enabled False in every completion
    payload -- a request sent right now never searches, live account or
    not."""
    assert capabilities.effective(ANONYMOUS)["search"] is False
    assert capabilities.effective(ACCOUNT)["search"] is False


def test_audio_transcription_is_true_with_credentials():
    assert capabilities.effective(ACCOUNT)["audio_transcription"] is True


def test_snapshot_reports_anonymous_with_no_credentials_configured(no_local_credentials):
    assert capabilities.snapshot().mode == "anonymous"


def test_snapshot_reports_account_with_email_and_password(no_local_credentials, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_EMAIL", "a@b.com")
    monkeypatch.setenv("DEEPSEEK_PASSWORD", "secret")
    assert capabilities.snapshot().mode == "account"


def test_snapshot_reports_account_with_a_cached_token_env_var_alone(
        no_local_credentials, monkeypatch):
    """DEEPSEEK_TOKEN lets _TokenManager skip login entirely -- it is its own
    sufficient credential, no email/password needed."""
    monkeypatch.setenv("DEEPSEEK_TOKEN", "some-cached-token")
    assert capabilities.snapshot().mode == "account"


def test_snapshot_reports_account_from_a_persisted_token_file_alone(no_local_credentials):
    """The gap the reviewer traced: docker-compose.yml mounts
    ~/.deepseek_token as a persistent cache, and _TokenManager / the deepseek
    CLI's save_token() can refresh it WHILE THE CONTAINER RUNS without ever
    touching os.environ (fixed at container start). A redeploy that rotates
    credentials out of the environment but keeps that mounted file must still
    report account -- the proxy is still serving those requests fine."""
    no_local_credentials.write_text("a-real-looking-cached-token")
    assert capabilities.snapshot().mode == "account"


def test_snapshot_reports_account_from_a_dot_env_token_alone(no_local_credentials):
    """The third of load_token()'s three local sources (deepseek:85-96), and
    the one a prior version of `no_local_credentials` left uncovered: a
    DEEPSEEK_TOKEN key written straight into .env rather than exported as an
    env var. save_token() (deepseek:99-104) writes here on every real login
    or refresh -- this repo's own .env carries that key, empty today. Without
    the fixture repointing _ds.ENV_FILE, this test (and every "anonymous"
    one) would read this repo's real .env instead of the isolated one."""
    with open(capabilities._ds.ENV_FILE, "w") as f:
        f.write("DEEPSEEK_TOKEN=a-token-written-by-save_token\n")
    assert capabilities.snapshot().mode == "account"


def test_snapshot_reports_anonymous_with_only_half_a_credential_pair(
        no_local_credentials, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_EMAIL", "a@b.com")
    assert capabilities.snapshot().mode == "anonymous"


def test_auth_block_never_invents_a_plan():
    block = capabilities.auth_block(ACCOUNT)
    assert block == {"mode": "account", "plan": None,
                      "subscription_active": False, "expires_at": None}


# --- /health -------------------------------------------------------------

import server as srv                                             # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402


def test_health_answers_without_an_api_key(no_local_credentials):
    client = TestClient(srv.app)
    r = client.get("/health")
    assert r.status_code == 200


def test_health_publishes_the_full_contract_shape(no_local_credentials):
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


def test_health_reports_account_mode_when_credentials_are_configured(
        no_local_credentials, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_EMAIL", "a@b.com")
    monkeypatch.setenv("DEEPSEEK_PASSWORD", "secret")
    client = TestClient(srv.app)
    body = client.get("/health").json()
    assert body["auth"]["mode"] == "account"
    assert body["capabilities"]["chat"] is True
    assert body["capabilities"]["tools"] is False


def test_health_reports_account_mode_from_a_persisted_token_file_alone(
        no_local_credentials):
    """Same gap as the snapshot()-level test above, exercised through the
    actual HTTP endpoint the gateway sweeps."""
    no_local_credentials.write_text("a-real-looking-cached-token")
    client = TestClient(srv.app)
    body = client.get("/health").json()
    assert body["auth"]["mode"] == "account"
    assert body["capabilities"]["chat"] is True


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
