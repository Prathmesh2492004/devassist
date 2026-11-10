"""Create the DevAssist GitHub App in two clicks, without copying any keys by hand.

GitHub's "app manifest" flow: this page sends GitHub a description of the app (name,
permissions, webhook URL). You confirm on github.com, GitHub sends you back here with a
one-time code, and the code is exchanged for the app's ID, private key and webhook secret,
which are saved to a git-ignored file that the API and the worker both read.

This is meant for running DevAssist on your own machine; a hosted deployment would set
GITHUB_APP_ID and friends as secrets instead.
"""

import html
import json
import logging
import secrets
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse

from app.config import Settings, get_settings
from app.review.app_auth import load_credentials
from app.review.github import GITHUB_API
from app.review.webhook_url import public_webhook_url

log = logging.getLogger(__name__)

router = APIRouter(prefix="/github-app", tags=["github"])

STATE_FILE = "github-app-state"  # next to the saved app


def get_github_http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))


SettingsDep = Annotated[Settings, Depends(get_settings)]
GitHubHttp = Annotated[httpx.Client, Depends(get_github_http)]


def manifest(settings: Settings, webhook_url: str) -> dict[str, object]:
    return {
        "name": f"DevAssist {secrets.token_hex(2)}",  # app names are unique across GitHub
        "url": settings.github_app_homepage,
        "description": "Reviews pull requests: Ruff bug rules plus a local AI model.",
        "hook_attributes": {"url": webhook_url, "active": True},
        "redirect_url": f"{settings.public_api_url}/github-app/callback",
        "public": False,  # only you can install it
        "default_permissions": {"pull_requests": "write", "contents": "read"},
        "default_events": ["pull_request"],
    }


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}
input{{font:inherit;padding:.4rem;width:100%;box-sizing:border-box}}
button,a.button{{font:inherit;padding:.5rem 1rem;margin-top:1rem;background:#1f6feb;color:#fff;
border:0;border-radius:6px;text-decoration:none;display:inline-block;cursor:pointer}}
code{{background:#eee;padding:0 .3rem;border-radius:4px}}</style></head>
<body><h1>{title}</h1>{body}</body></html>"""
    )


def _install_link(slug: str | None) -> str:
    if not slug:
        return "<p>Install it from the app's page on GitHub (Settings → Developer settings).</p>"
    url = f"https://github.com/apps/{html.escape(slug)}/installations/new"
    return (
        "<p>Next, install it on the repositories DevAssist should review. Pick only the ones "
        "you've added to DevAssist.</p>"
        f'<a class="button" href="{url}">Install the app on GitHub</a>'
    )


@router.get("/setup", response_class=HTMLResponse)
def setup(settings: SettingsDep, http: GitHubHttp) -> HTMLResponse:
    credentials = load_credentials(settings)
    if credentials is not None:
        return _page(
            "The GitHub App is set up",
            f"<p>App ID <code>{html.escape(credentials.app_id)}</code> is ready.</p>"
            + _install_link(credentials.slug),
        )
    webhook_url = public_webhook_url(settings, client=http)
    if webhook_url is None:
        return _page(
            "One step first",
            "<p>GitHub can't reach <code>localhost</code>, so it needs a public address for "
            "this API. Start the free Cloudflare tunnel with "
            "<code>docker compose --profile github-app up -d</code>, wait a few seconds, "
            "then reload this page.</p>",
        )

    state = secrets.token_urlsafe(24)
    state_path = Path(settings.github_app_file).parent / STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state, encoding="utf-8")
    app = manifest(settings, webhook_url)
    return _page(
        "Create the DevAssist GitHub App",
        "<p>GitHub will show you the app's permissions: read code, and write comments on "
        "pull requests. It's private, free, and only works where you install it.</p>"
        f'<form method="post" action="https://github.com/settings/apps/new?state={state}">'
        '<label for="name">App name (must be unique on GitHub)</label>'
        f'<input id="name" value="{html.escape(str(app["name"]))}" required>'
        f'<input type="hidden" name="manifest" id="manifest" '
        f'value="{html.escape(json.dumps(app))}">'
        '<button type="submit">Create on GitHub</button></form>'
        "<script>document.querySelector('form').addEventListener('submit', () => {"
        "const field = document.getElementById('manifest');"
        "const app = JSON.parse(field.value);"
        "app.name = document.getElementById('name').value.trim() || app.name;"
        "field.value = JSON.stringify(app);});</script>",
    )


@router.get("/callback", response_class=HTMLResponse)
def callback(code: str, state: str, settings: SettingsDep, http: GitHubHttp) -> HTMLResponse:
    saved_path = Path(settings.github_app_file)
    state_path = saved_path.parent / STATE_FILE
    try:
        expected = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        expected = ""
    # The state proves this callback continues a setup started on this machine.
    if not expected or not secrets.compare_digest(state, expected):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Setup expired. Open /github-app/setup again."
        )

    try:
        response = http.post(
            f"{GITHUB_API}/app-manifests/{code}/conversions",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "devassist"},
        )
    except httpx.TransportError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not reach GitHub") from exc
    if response.is_error:
        log.warning("App manifest conversion failed: %s", response.status_code)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "GitHub didn't accept the code. Start the setup again."
        )
    app = response.json()
    saved = {k: app[k] for k in ("id", "slug", "pem", "webhook_secret", "html_url") if k in app}
    saved_path.parent.mkdir(parents=True, exist_ok=True)
    saved_path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    saved_path.chmod(0o600)  # the private key: readable by you only
    state_path.unlink(missing_ok=True)
    log.info("Saved GitHub App %s (%s)", saved.get("id"), saved.get("slug"))
    return _page(
        "The GitHub App is created",
        "<p>Its private key and webhook secret are saved in "
        f"<code>api/{html.escape(settings.github_app_file)}</code>, which git ignores. "
        "Never share that file.</p>" + _install_link(saved.get("slug")),
    )
