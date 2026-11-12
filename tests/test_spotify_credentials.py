"""Tests for the Telegram-managed Spotify credential file."""

import stat
from pathlib import Path

import pytest

from spotify_credentials import (
    CredentialsError,
    clear_credentials,
    describe_credentials,
    effective_credentials,
    fetch_client_credentials_token,
    load_credentials,
    mask,
    probe_official,
    save_credentials,
    set_access_token,
    set_client_credentials,
    set_market,
    test_credentials as check_credentials,
)

ENV_KEYS = (
    "SPOTIFY_ACCESS_TOKEN",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_MARKET",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_missing_file_is_empty(tmp_path: Path):
    assert load_credentials(tmp_path / "missing.json") == {
        "client_id": "",
        "client_secret": "",
        "access_token": "",
        "market": "",
        "updated_at": "",
    }


def test_access_token_saved_with_private_permissions(tmp_path: Path):
    path = tmp_path / "spotify.json"
    set_access_token(path, "BQ" + "x" * 40)

    data = load_credentials(path)
    assert data["access_token"].startswith("BQ")
    assert data["client_id"] == ""
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    effective = effective_credentials(path, env={})
    assert effective["mode"] == "token"
    assert effective["source"] == "telegram"


def test_client_credentials_clear_access_token(tmp_path: Path):
    path = tmp_path / "spotify.json"
    set_access_token(path, "BQ" + "x" * 40)
    set_client_credentials(path, "a" * 32, "b" * 32)

    data = load_credentials(path)
    assert data["access_token"] == ""
    assert data["client_id"] == "a" * 32
    assert data["client_secret"] == "b" * 32
    assert effective_credentials(path, env={})["mode"] == "client"


def test_file_values_override_env(tmp_path: Path):
    path = tmp_path / "spotify.json"
    set_client_credentials(path, "file-id", "file-secret")
    env = {
        "SPOTIFY_CLIENT_ID": "env-id",
        "SPOTIFY_CLIENT_SECRET": "env-secret",
        "SPOTIFY_MARKET": "US",
    }
    effective = effective_credentials(path, env=env)
    assert effective["client_id"] == "file-id"
    assert effective["client_secret"] == "file-secret"
    assert effective["market"] == "US"
    assert effective["source"] == "telegram"


def test_env_only_and_anonymous(tmp_path: Path):
    env = {"SPOTIFY_ACCESS_TOKEN": "env-token"}
    anonymous = effective_credentials(tmp_path / "missing.json", env={})
    assert anonymous["mode"] == "anonymous"
    assert anonymous["source"] == "none"

    from_env = effective_credentials(tmp_path / "missing.json", env=env)
    assert from_env["mode"] == "token"
    assert from_env["source"] == "env"
    assert from_env["access_token"] == "env-token"


def test_clear_removes_file(tmp_path: Path):
    path = tmp_path / "spotify.json"
    set_access_token(path, "BQ" + "x" * 40)
    assert clear_credentials(path) is True
    assert not path.exists()
    assert clear_credentials(path) is False


def test_market_preserves_credentials(tmp_path: Path):
    path = tmp_path / "spotify.json"
    set_access_token(path, "BQ" + "x" * 40)
    set_market(path, "it")
    data = load_credentials(path)
    assert data["market"] == "IT"
    assert data["access_token"].startswith("BQ")


def test_status_never_prints_secrets(tmp_path: Path):
    path = tmp_path / "spotify.json"
    token = "BQ" + "x" * 40
    secret = "s" * 32
    set_client_credentials(path, "a" * 32, secret)
    text = describe_credentials(path)
    assert token not in text
    assert secret not in text
    assert "access token" not in text.lower() or "set" in text.lower()
    assert "client secret: set" in text.lower()


def test_mask_short_and_long_values():
    assert mask("short") == "…"
    assert mask("abcdefghij", keep=2) == "ab…ij"


def test_test_credentials_anonymous(tmp_path: Path):
    assert "Anonymous mode" in check_credentials(tmp_path / "missing.json")


def test_test_credentials_uses_client_credentials(tmp_path: Path, monkeypatch):
    path = tmp_path / "spotify.json"
    set_client_credentials(path, "id", "secret")
    calls = []

    def fake_fetch(client_id, client_secret):
        calls.append((client_id, client_secret))
        return "token"

    def fake_probe(token):
        calls.append(("probe", token))

    monkeypatch.setattr(
        "spotify_credentials.fetch_client_credentials_token", fake_fetch
    )
    monkeypatch.setattr("spotify_credentials.probe_official", fake_probe)
    assert "client credentials" in check_credentials(path)
    assert calls == [("id", "secret"), ("probe", "token")]


def test_fetch_token_success(monkeypatch):
    monkeypatch.setattr(
        "spotify_credentials._request_json",
        lambda *a, **k: (200, {"access_token": "token"}),
    )
    assert fetch_client_credentials_token("id", "secret") == "token"


def test_fetch_token_rejects_http_error(monkeypatch):
    monkeypatch.setattr(
        "spotify_credentials._request_json", lambda *a, **k: (401, {})
    )
    with pytest.raises(CredentialsError):
        fetch_client_credentials_token("id", "secret")


def test_probe_maps_http_status(monkeypatch):
    for status in (401, 429, 500):
        monkeypatch.setattr(
            "spotify_credentials._request_json", lambda *a, **k: (status, {})
        )
        with pytest.raises(CredentialsError):
            probe_official("token")


def test_probe_success(monkeypatch):
    monkeypatch.setattr(
        "spotify_credentials._request_json", lambda *a, **k: (200, {})
    )
    probe_official("token")


def test_fetch_and_probe_raise_safe_errors(monkeypatch):
    def offline(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr("spotify_credentials._request_json", offline)
    with pytest.raises(CredentialsError):
        fetch_client_credentials_token("id", "secret")
    with pytest.raises(CredentialsError):
        probe_official("token")
