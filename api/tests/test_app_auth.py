"""Signing in as the GitHub App, with a throwaway key made for each test run."""

import json
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.review.app_auth import (
    JWT_BACKDATE_SECONDS,
    JWT_LIFETIME_SECONDS,
    AppCredentials,
    app_jwt,
    installation_token,
    load_credentials,
)
from app.review.github import GitHubError


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def credentials(private_key: rsa.RSAPrivateKey) -> AppCredentials:
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return AppCredentials(app_id="123", private_key=pem, webhook_secret="s3cret", slug="devassist")


def test_the_jwt_is_signed_by_the_app_and_short_lived(
    credentials: AppCredentials, private_key: rsa.RSAPrivateKey
) -> None:
    token = app_jwt(credentials, now=1_000_000)

    claims = jwt.decode(token, private_key.public_key(), algorithms=["RS256"],
                        options={"verify_exp": False})  # fmt: skip
    assert claims["iss"] == "123"
    assert claims["iat"] == 1_000_000 - JWT_BACKDATE_SECONDS
    assert claims["exp"] - claims["iat"] == JWT_LIFETIME_SECONDS <= 600


def test_trades_the_jwt_for_an_installation_token(credentials: AppCredentials) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"token": "ghs_installation"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert installation_token(credentials, 42, client=client) == "ghs_installation"
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/app/installations/42/access_tokens"
    assert seen[0].headers["Authorization"].startswith("Bearer ey")


def test_an_uninstalled_app_is_a_readable_error(credentials: AppCredentials) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(GitHubError, match="uninstalled"):
        installation_token(credentials, 42, client=client)


def test_reads_the_app_saved_by_the_setup_page(tmp_path: Path) -> None:
    saved = tmp_path / "github-app.json"
    saved.write_text(json.dumps({"id": 9, "slug": "dev", "pem": "KEY", "webhook_secret": "w"}))

    loaded = load_credentials(Settings(github_app_file=str(saved)))

    assert loaded == AppCredentials(app_id="9", private_key="KEY", webhook_secret="w", slug="dev")


def test_no_app_until_one_is_set_up(tmp_path: Path) -> None:
    assert load_credentials(Settings(github_app_file=str(tmp_path / "missing.json"))) is None
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"id": 9}))
    assert load_credentials(Settings(github_app_file=str(partial))) is None


def test_environment_variables_win_over_the_saved_file(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_text("PEM")
    settings = Settings(
        github_app_id="77",
        github_app_private_key_path=str(key),
        github_webhook_secret="env-secret",
        github_app_file=str(tmp_path / "missing.json"),
    )

    assert load_credentials(settings) == AppCredentials("77", "PEM", "env-secret")
