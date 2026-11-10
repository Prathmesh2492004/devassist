"""Sign in as the DevAssist GitHub App.

A GitHub App has its own identity, so its comments show up as "DevAssist[bot]", not as you.
To act on a repository it:

1. signs a short-lived JWT with the app's private key (proves "I am app 123"), then
2. trades it for an installation token, which works only on the repositories where someone
   installed the app, and expires after an hour.

The app's ID, private key and webhook secret come from environment variables, or from the
file the setup page (routes/github_app.py) writes. That file is git-ignored.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import httpx
import jwt

from app.config import Settings
from app.review.github import GITHUB_API, GitHubError

log = logging.getLogger(__name__)

# GitHub rejects app JWTs that live longer than 10 minutes. Backdating covers clock drift.
JWT_LIFETIME_SECONDS = 9 * 60
JWT_BACKDATE_SECONDS = 60


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    private_key: str
    webhook_secret: str
    slug: str | None = None  # the app's URL name, for links such as "install the app"


class _SavedApp(TypedDict, total=False):
    id: int
    slug: str
    pem: str
    webhook_secret: str


def load_credentials(settings: Settings) -> AppCredentials | None:
    """The app's credentials, or None when no GitHub App is set up yet."""
    if settings.github_app_id and settings.github_webhook_secret:
        key_path = settings.github_app_private_key_path
        try:
            private_key = Path(key_path).read_text(encoding="utf-8") if key_path else ""
        except OSError as exc:
            log.warning("Can't read GITHUB_APP_PRIVATE_KEY_PATH: %s", exc)
            return None
        return AppCredentials(
            app_id=settings.github_app_id,
            private_key=private_key,
            webhook_secret=settings.github_webhook_secret.get_secret_value(),
        )
    return read_saved_app(Path(settings.github_app_file))


def read_saved_app(path: Path) -> AppCredentials | None:
    try:
        saved: _SavedApp = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Can't read the saved GitHub App in %s: %s", path, exc)
        return None
    if not all(k in saved for k in ("id", "pem", "webhook_secret")):
        log.warning("The saved GitHub App in %s is incomplete", path)
        return None
    return AppCredentials(
        app_id=str(saved["id"]),
        private_key=saved["pem"],
        webhook_secret=saved["webhook_secret"],
        slug=saved.get("slug"),
    )


def app_jwt(credentials: AppCredentials, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time()) - JWT_BACKDATE_SECONDS
    claims = {"iat": issued, "exp": issued + JWT_LIFETIME_SECONDS, "iss": credentials.app_id}
    return jwt.encode(claims, credentials.private_key, algorithm="RS256")


def installation_token(
    credentials: AppCredentials, installation_id: int, *, client: httpx.Client | None = None
) -> str:
    """A token that acts as the app on the repositories of one installation."""
    http = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        response = http.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt(credentials)}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "devassist",
            },
        )
    except httpx.TransportError as exc:
        raise GitHubError("Could not reach GitHub. Check your connection.") from exc
    if response.status_code in (401, 404):
        raise GitHubError(
            "GitHub didn't accept the DevAssist app. It may have been uninstalled, "
            "or its private key changed."
        )
    if response.is_error:
        raise GitHubError(f"GitHub returned an error ({response.status_code}).")
    token: str = response.json()["token"]
    return token
