"""The setup page that creates the GitHub App through GitHub's manifest flow."""

import html
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.routes.github_app import STATE_FILE, get_github_http

PUBLIC = "https://quiet-river.trycloudflare.com/webhooks/github"
CREATED = {
    "id": 9,
    "slug": "devassist-ab12",
    "pem": "KEY",
    "webhook_secret": "w",
    "client_secret": "not kept",
}


def _client(
    tmp_path: Path, transport: httpx.MockTransport | None = None, proxy: str | None = PUBLIC
) -> TestClient:
    app = create_app()
    config = Settings(github_app_file=str(tmp_path / "github-app.json"), github_webhook_url=proxy)
    app.dependency_overrides[get_settings] = lambda: config
    if transport is not None:
        app.dependency_overrides[get_github_http] = lambda: httpx.Client(transport=transport)
    return TestClient(app)


def test_asks_for_a_webhook_relay_first(tmp_path: Path) -> None:
    page = _client(tmp_path, proxy=None).get("/github-app/setup").text
    assert "--profile github-app" in page


def test_sends_github_a_manifest_with_the_permissions_reviews_need(tmp_path: Path) -> None:
    page = _client(tmp_path).get("/github-app/setup").text

    state = (tmp_path / STATE_FILE).read_text()
    assert f'action="https://github.com/settings/apps/new?state={state}"' in page
    match = re.search(r'name="manifest" id="manifest" value="([^"]*)"', page)
    assert match is not None
    manifest = json.loads(html.unescape(match.group(1)))
    assert manifest["hook_attributes"]["url"] == PUBLIC
    assert manifest["redirect_url"] == "http://localhost:8000/github-app/callback"
    assert manifest["default_permissions"] == {"pull_requests": "write", "contents": "read"}
    assert manifest["default_events"] == ["pull_request"]
    assert manifest["public"] is False


def test_the_callback_saves_the_new_app_and_links_to_installing_it(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=CREATED)

    client = _client(tmp_path, httpx.MockTransport(handler))
    client.get("/github-app/setup")
    state = (tmp_path / STATE_FILE).read_text()

    page = client.get("/github-app/callback", params={"code": "one-time", "state": state}).text

    assert seen[0].url.path == "/app-manifests/one-time/conversions"
    saved = json.loads((tmp_path / "github-app.json").read_text())
    assert saved == {"id": 9, "slug": "devassist-ab12", "pem": "KEY", "webhook_secret": "w"}
    assert "https://github.com/apps/devassist-ab12/installations/new" in page
    assert not (tmp_path / STATE_FILE).exists()
    # Once saved, the setup page shows the app instead of creating another.
    assert "is set up" in client.get("/github-app/setup").text


@pytest.mark.parametrize("state", ["forged", ""])
def test_the_callback_needs_the_state_from_this_machine(tmp_path: Path, state: str) -> None:
    client = _client(tmp_path)
    client.get("/github-app/setup")

    res = client.get("/github-app/callback", params={"code": "x", "state": state})

    assert res.status_code == 400
    assert not (tmp_path / "github-app.json").exists()
