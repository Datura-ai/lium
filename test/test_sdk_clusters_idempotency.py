"""Cluster rent idempotency (lium#328), kept from test_sdk_clusters.py by DAH-4027: one Idempotency-Key per order, one
resend where the backend honours it, and an unanswered order stays an uncertain rent instead of a second one.
"""

import json
import time
import uuid
from types import SimpleNamespace

import pytest

from lium.sdk import (
    Config, Lium, LiumError, LiumPermissionError, Template,
)
from lium.sdk.exceptions import LiumAuthError, LiumServerError

FABRIC = "hot:infiniband:0x3:0x7fff:NVIDIA H100 80GB HBM3:8"


def _node(i: int, price: float = 2.0, gpu_count: int = 8, name: str = "NVIDIA H100 80GB HBM3") -> dict:
    return {
        "id": f"exec-{i}", "machine_name": name, "price_per_gpu": price,
        "specs": {"gpu": {"count": gpu_count, "details": [{"name": name}]}},
        "location": {"country": "US"}, "executor_ip_address": f"10.0.0.{i}", "status": "active", "tier": "secure",
    }


def _offer_payload(free: int = 3, total: int = 4) -> dict:
    return {
        "fabric_id": FABRIC, "fabric_type": "infiniband", "link_rate": "400 Gb/sec (4X NDR)", "fabric_measured": True,
        "node_count": total, "nodes": [_node(i, price=2.0 + i) for i in range(free)],
    }


def _pod_payload(i: int, cluster_id: str = "c-1", name: str = "job", status: str = "RUNNING") -> dict:
    return {
        "id": f"pod-{i}", "pod_name": name, "status": status,
        "ssh_connect_cmd": f"ssh root@1.2.3.{i} -p 2200{i}" if status == "RUNNING" else None,
        "ports_mapping": {"22": 22000 + i}, "created_at": "", "updated_at": "", "price": 16.0,
        "executor": _node(i), "template": {"id": "tpl-c"},
        "cluster_id": cluster_id, "cluster_node_index": i, "cluster_overlay_ip": f"10.42.0.{i + 1}",
    }


class _Config(Config):
    """A config with a fixed public key instead of one read from disk."""

    keys = ["ssh-ed25519 AAAA test"]

    @property
    def ssh_public_keys(self):
        return list(self.keys)


class _Client(Lium):
    """A client whose HTTP layer is a scripted dict of endpoint -> responses."""

    def __init__(self, routes=None, ps_sequence=None):
        super().__init__(_Config(api_key="test"))
        self.routes = routes or {}
        self.calls: list = []
        self._ps_sequence = list(ps_sequence or [])

    def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs.get("json")))
        answer = self.routes.get((method, endpoint))
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer()
        return SimpleNamespace(json=lambda: answer)

    def ps(self):
        """One listing per call from the sequence (the last one repeats); an Exception in the sequence is raised."""
        self.calls.append(("ps",))
        if not self._ps_sequence:
            return []
        answer = self._ps_sequence.pop(0) if len(self._ps_sequence) > 1 else self._ps_sequence[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _ensure_ssh_keys_registered(self, public_keys, name=None):
        self.calls.append(("ssh-keys", tuple(public_keys)))


def _pods(payloads):
    """Run payloads through the real ps() parser once, so the cluster fields are exercised."""
    c = _Client(routes={("GET", "/pods"): payloads})
    return Lium.ps(c)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


# --- parsing --------------------------------------------------------------------------------------

def _template(tag: str, image: str = "daturaai/lium-cluster") -> Template:
    return Template(id=f"tpl-{tag}", name="Multi-node cluster", huid="h", docker_image=image, docker_image_tag=tag,
                    category="PYTORCH", status="VERIFY_SUCCESS")


class _HttpResponse:
    text = ""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = {}
        self._body = body

    def json(self):
        return self._body


def _member_row(i: int, success: bool = True, message: str = "Pod deletion started") -> dict:
    return {"pod_id": f"pod-{i}", "pod_name": "job", "cluster_node_index": i, "success": success, "message": message}


def _error_body(status: int, message: str) -> dict:
    """The platform's error envelope (DAH-3056): `message` is the route's detail."""
    return {"success": False, "error": {"code": "x", "message": message, "hint": None, "request_id": "r-1"},
            "message": message, "status_code": status}


def _http_client(monkeypatch, tmp_path, response):
    """A client whose one HTTP call answers `response`; `calls` records (method, url). HOME is `tmp_path`, so the
    pinned host keys the call may drop live there."""
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        return response

    monkeypatch.setattr("lium.sdk.client.requests.request", fake_request)
    client = Lium(_Config(api_key="test"))
    client.calls = calls
    return client


def test_up_cluster_sends_an_idempotency_key(monkeypatch):
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): {"success": True, "pod_ids": ["pod-0", "pod-1"]}},
        ps_sequence=[_pods([_pod_payload(0), _pod_payload(1)])],
    )
    monkeypatch.setattr(client, "cluster_template", lambda: _template("0.0.7"))
    sent_headers = []
    record = client._request
    monkeypatch.setattr(
        client, "_request", lambda method, endpoint, **kw: sent_headers.append(kw.get("headers")) or record(method, endpoint, **kw)
    )

    client.up_cluster(["exec-0", "exec-1"], name="job")

    [headers] = [h for h in sent_headers if h is not None]
    uuid.UUID(headers["Idempotency-Key"])
    assert headers["X-API-KEY"] == "test"


def test_up_cluster_resends_once_with_the_same_key_when_the_backend_honours_it(monkeypatch):
    answers = iter([LiumServerError("Server error: 504"), {"success": True, "pod_ids": ["pod-0", "pod-1"]}])

    def order():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = _Client(
        routes={("GET", "/version"): {"features": ["rent_idempotency"]}, ("POST", "/executors/cluster/rent"): order},
        ps_sequence=[[], _pods([_pod_payload(0), _pod_payload(1)])],
    )
    sent_keys = []
    record = client._request
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, endpoint, **kw: sent_keys.append((kw.get("headers") or {}).get("Idempotency-Key"))
        or record(method, endpoint, **kw),
    )

    cluster = client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert cluster.id == "c-1"
    posts = [c for c in client.calls if c[0] == "POST"]
    assert len(posts) == 2 and posts[0][2] == posts[1][2]
    post_keys = [k for k in sent_keys if k is not None]
    assert len(post_keys) == 2 and post_keys[0] == post_keys[1]


@pytest.mark.parametrize(
    "resend_error",
    [
        LiumError("conflict", code="idempotency_in_progress"),
        LiumError("conflict", code="idempotency_outcome_unknown"),
        # lium#328 r4285561625: an auth or balance refusal refuses the resend only
        LiumAuthError("Invalid API key"),
        LiumPermissionError("Insufficient balance"),
    ],
)
def test_up_cluster_looks_up_by_name_when_the_resend_cannot_replay(resend_error):
    answers = iter([LiumServerError("Server error: 504"), resend_error])

    def order():
        raise next(answers)

    client = _Client(
        routes={("GET", "/version"): {"features": ["rent_idempotency"]}, ("POST", "/executors/cluster/rent"): order},
        ps_sequence=[[], _pods([_pod_payload(0), _pod_payload(1)])],
    )

    cluster = client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert cluster.id == "c-1"
    assert len([c for c in client.calls if c[0] == "POST"]) == 2


_TIMEOUT = object()   # the resend times out as well


@pytest.mark.parametrize("resend", [_TIMEOUT, "idempotency_in_progress", "idempotency_outcome_unknown"])
@pytest.mark.parametrize("listing", ["empty", "partial", "failed", "complete"])
def test_up_cluster_unanswered_order_stays_uncertain_through_the_cli(monkeypatch, tmp_path, resend, listing):
    """The first order times out and the resend cannot replay it. Only a whole new cluster settles the outcome; an
    empty, partial or failed listing must stay uncertain (the order may still land), so the CLI must report
    cluster_rent_uncertain and never 'Retry', which would rent a second cluster (lium#328 r4285299647)."""
    from click.testing import CliRunner

    from lium.cli.cli import cli
    from lium.cli.clusters import command as clusters_command

    answers = iter([
        LiumServerError("Server error: 504"),
        LiumServerError("Server error: 504") if resend is _TIMEOUT else LiumError("conflict", code=resend),
    ])

    def order():
        raise next(answers)

    after = {
        "empty": [], "partial": _pods([_pod_payload(0)]), "failed": LiumServerError("Server error: 503"),
        "complete": _pods([_pod_payload(0), _pod_payload(1)]),
    }[listing]
    client = _Client(
        routes={
            ("GET", "/version"): {"features": ["rent_idempotency"]},
            ("GET", "/executors/infiniband-clusters"): [_offer_payload()],
            ("POST", "/executors/cluster/rent"): order,
        },
        ps_sequence=[[], after],
    )
    monkeypatch.setattr(client, "cluster_template", lambda: SimpleNamespace(id="tpl-c"))
    monkeypatch.setattr(clusters_command, "Lium", lambda *a, **k: client)
    monkeypatch.setattr(clusters_command, "ensure_config", lambda: None)

    result = CliRunner().invoke(
        cli, ["clusters", "up", FABRIC, "--nodes", "2", "-n", "job", "-y", "--format", "json"], catch_exceptions=False,
    )

    assert len([c for c in client.calls if c[0] == "POST"]) == 2
    if listing == "complete":
        assert result.exit_code == 0, result.output
        return
    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "cluster_rent_uncertain"
    assert "Retry" not in result.output and "may be rented" in result.output
