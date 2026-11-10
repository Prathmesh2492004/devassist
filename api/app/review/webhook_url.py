"""Keep the GitHub App's webhook pointed at this machine.

GitHub can only deliver events to a public HTTPS address. Locally that's a Cloudflare quick
tunnel (docker compose --profile github-app), whose address is random and changes on every
start. The tunnel reports it on its metrics port, and the app's webhook address can be
changed through GitHub's API, so the API checks every so often and updates it when it moves.

A fixed address (GITHUB_WEBHOOK_URL, for example on a real server) skips all of this.
"""

import asyncio
import logging
from typing import TypedDict

import httpx

from app.config import Settings
from app.review.app_auth import AppCredentials, app_jwt, load_credentials
from app.review.github import GITHUB_API, GitHubError

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/github"
CHECK_EVERY_SECONDS = 30
RESTART_HINT = "docker compose --profile github-app restart tunnel"


class _QuickTunnel(TypedDict):
    hostname: str


def tunnel_hostname(metrics_url: str, *, client: httpx.Client) -> str | None:
    """The quick tunnel's public hostname, or None while it isn't connected.

    A quick tunnel can be dropped by Cloudflare. cloudflared then keeps running and keeps
    reporting the old hostname, so /ready (connections to Cloudflare) is checked first.
    """
    try:
        if client.get(f"{metrics_url}/ready", timeout=5).status_code != 200:
            return None
        response = client.get(f"{metrics_url}/quicktunnel", timeout=5)
        data: _QuickTunnel = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return data.get("hostname") or None


def public_webhook_url(settings: Settings, *, client: httpx.Client) -> str | None:
    if settings.github_webhook_url:
        return settings.github_webhook_url
    if settings.tunnel_metrics_url:
        hostname = tunnel_hostname(settings.tunnel_metrics_url, client=client)
        if hostname:
            return f"https://{hostname}{WEBHOOK_PATH}"
    return None


def point_app_at(credentials: AppCredentials, url: str, *, client: httpx.Client) -> None:
    """Change where GitHub sends the app's events. Needs the app's JWT, not a user token."""
    try:
        response = client.patch(
            f"{GITHUB_API}/app/hook/config",
            json={"url": url, "content_type": "json"},
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt(credentials)}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "devassist",
            },
        )
    except httpx.TransportError as exc:
        raise GitHubError("Could not reach GitHub. Check your connection.") from exc
    if response.is_error:
        raise GitHubError(f"GitHub didn't update the webhook address ({response.status_code}).")


class WebhookUrlKeeper:
    """Remembers the last address it set, so GitHub is only called when it changes."""

    def __init__(self, settings: Settings, client: httpx.Client) -> None:
        self._settings = settings
        self._client = client
        self.current: str | None = None
        self._warned_down = False

    def check(self) -> str | None:
        credentials = load_credentials(self._settings)
        if credentials is None:
            return None
        url = public_webhook_url(self._settings, client=self._client)
        if url is None:
            if not self._warned_down:
                log.warning(
                    "The tunnel isn't connected, so GitHub can't deliver events. "
                    "Start or restart it with: %s",
                    RESTART_HINT,
                )
                self._warned_down = True
            return None
        self._warned_down = False
        if url == self.current:
            return url
        try:
            point_app_at(credentials, url, client=self._client)
        except GitHubError as exc:
            log.warning("Couldn't point the GitHub App at %s: %s", url, exc)
            return None
        log.info("GitHub App events now go to %s", url)
        self.current = url
        return url


async def keep_webhook_url_current(keeper: WebhookUrlKeeper) -> None:
    while True:
        try:
            await asyncio.to_thread(keeper.check)
        except Exception:  # never let this loop take the API down
            log.exception("Checking the webhook address failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
