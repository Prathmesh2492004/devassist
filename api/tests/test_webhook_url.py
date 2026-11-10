import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.review.webhook_url import WebhookUrlKeeper, public_webhook_url, tunnel_hostname

METRICS = "http://tunnel:2000"


class FakeNetwork:
    """The tunnel's metrics port and GitHub's API, with a hostname that can change."""

    def __init__(self, hostname: str | None = "quiet-river.trycloudflare.com") -> None:
        self.hostname = hostname
        self.patched: list[dict[str, str]] = []
        self.github_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "tunnel":
            if self.hostname is None:
                raise httpx.ConnectError("tunnel isn't running")
            return httpx.Response(200, json={"hostname": self.hostname})
        assert (request.method, request.url.path) == ("PATCH", "/app/hook/config")
        assert request.headers["Authorization"].startswith("Bearer ")
        self.patched.append(json.loads(request.content))
        return httpx.Response(self.github_status, json={})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    saved = tmp_path / "github-app.json"
    saved.write_text(json.dumps({"id": 9, "pem": pem, "webhook_secret": "w"}))
    return Settings(github_app_file=str(saved), tunnel_metrics_url=METRICS)


def test_reads_the_tunnel_address() -> None:
    network = FakeNetwork()
    assert tunnel_hostname(METRICS, client=network.client()) == "quiet-river.trycloudflare.com"
    network.hostname = None
    assert tunnel_hostname(METRICS, client=network.client()) is None


def test_a_fixed_address_wins_over_the_tunnel() -> None:
    fixed = Settings(github_webhook_url="https://devassist.example.com/webhooks/github",
                     tunnel_metrics_url=METRICS)  # fmt: skip
    assert public_webhook_url(fixed, client=FakeNetwork().client()) == fixed.github_webhook_url


def test_points_the_app_at_the_tunnel_only_when_the_address_changes(settings: Settings) -> None:
    network = FakeNetwork()
    keeper = WebhookUrlKeeper(settings, network.client())

    first = keeper.check()
    keeper.check()  # same address: GitHub isn't called again
    network.hostname = "new-name.trycloudflare.com"  # the tunnel restarted
    second = keeper.check()

    assert first == "https://quiet-river.trycloudflare.com/webhooks/github"
    assert second == "https://new-name.trycloudflare.com/webhooks/github"
    assert [p["url"] for p in network.patched] == [first, second]
    assert network.patched[0]["content_type"] == "json"


def test_retries_when_github_refuses(settings: Settings) -> None:
    network = FakeNetwork()
    network.github_status = 500
    keeper = WebhookUrlKeeper(settings, network.client())

    assert keeper.check() is None
    network.github_status = 200
    assert keeper.check() is not None
    assert len(network.patched) == 2


def test_does_nothing_without_an_app_or_a_tunnel(tmp_path: Path, settings: Settings) -> None:
    no_app = Settings(github_app_file=str(tmp_path / "missing.json"), tunnel_metrics_url=METRICS)
    network = FakeNetwork()
    assert WebhookUrlKeeper(no_app, network.client()).check() is None

    network.hostname = None
    assert WebhookUrlKeeper(settings, network.client()).check() is None
    assert network.patched == []
