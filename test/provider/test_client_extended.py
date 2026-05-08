"""SDK tests for extended ``ProviderClient`` methods.

Covers methods added to drive the full provider-portal workflow from the
CLI/agent side:

- profile / config (set-email, opt-in/out, dashboard)
- executor lifecycle (add, delete, update-price, update-gpu, min-gpu,
  notice-period, notify-added)
- batch sync (from/to miner server)
- read-only queries (validators, billing, machines, machine-requests)
"""

from __future__ import annotations

from typing import Any

import pytest

from lium.provider.client import ProviderClient
from lium.provider.errors import ProviderError
from lium.provider.models import OptInStatusResponse


class _Portal:
    def __init__(self, *, get_body=None, post_body=None, delete_body=None):
        self._get_body = get_body
        self._post_body = post_body
        self._delete_body = delete_body
        self.posts: list[Any] = []
        self.gets: list[Any] = []
        self.deletes: list[Any] = []

    def get(self, path, *, params=None, auth=True):
        self.gets.append((path, params, auth))
        return self._get_body if self._get_body is not None else {}

    def post(self, path, *, json_body=None, auth=True):
        self.posts.append((path, json_body, auth))
        return self._post_body if self._post_body is not None else {}

    def put(self, *a, **k):  # pragma: no cover
        return {}

    def delete(self, path, *, auth=True):
        self.deletes.append((path, auth))
        return self._delete_body if self._delete_body is not None else {}


@pytest.fixture
def client(fake_signer, tmp_token_store):
    def _build(portal: _Portal) -> ProviderClient:
        return ProviderClient(
            signer=fake_signer,
            token_store=tmp_token_store,
            http=portal,  # type: ignore[arg-type]
        )

    return _build


# ---------------------------------------------------------------------------
# Profile / config


def test_set_email_posts_payload(client) -> None:
    portal = _Portal(post_body={"data": {}})
    c = client(portal)
    c.set_email("a@b.co")
    assert portal.posts[0] == ("/auth/set-email", {"email": "a@b.co"}, True)


def test_set_machine_request_subscription(client) -> None:
    portal = _Portal(post_body={"data": {}})
    c = client(portal)
    c.set_machine_request_subscription(["H100", "A100"])
    assert portal.posts[0] == (
        "/auth/set-machine-request-subscription",
        {"machine_request_subscription": ["H100", "A100"]},
        True,
    )


def test_set_opt_in_status_returns_typed_response(client) -> None:
    portal = _Portal(
        post_body={
            "miner_hotkey": "5Foo",
            "miner_coldkey": "5Bar",
            "central_miner_ip": "1.2.3.4",
            "central_miner_port": 9090,
        }
    )
    c = client(portal)
    response = c.set_opt_in_status(True)
    assert isinstance(response, OptInStatusResponse)
    assert response.central_miner_ip == "1.2.3.4"
    assert response.central_miner_port == 9090
    assert portal.posts[0][1] == {"opt_in_status": True}


def test_set_opt_in_drift_raises_provider_error(client) -> None:
    portal = _Portal(post_body={"unexpected": "shape"})
    c = client(portal)
    with pytest.raises(ProviderError) as exc_info:
        c.set_opt_in_status(True)
    assert exc_info.value.code == "PORTAL_CONTRACT_DRIFT"


# ---------------------------------------------------------------------------
# Node lifecycle


def test_add_node_posts_payload(client) -> None:
    portal = _Portal(post_body={"message": "queued"})
    c = client(portal)
    c.add_node(
        gpu_type="H100",
        ip_address="10.0.0.1",
        port=8080,
        price_per_gpu=2.5,
        gpu_count=4,
    )
    assert portal.posts[0][0] == "/executors"
    assert portal.posts[0][1] == {
        "gpu_type": "H100",
        "ip_address": "10.0.0.1",
        "port": 8080,
        "price_per_gpu": 2.5,
        "gpu_count": 4,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        # gpu_count below 1
        dict(
            gpu_type="H100",
            ip_address="1.1.1.1",
            port=8080,
            price_per_gpu=1.0,
            gpu_count=0,
        ),
        # gpu_count above 64
        dict(
            gpu_type="H100",
            ip_address="1.1.1.1",
            port=8080,
            price_per_gpu=1.0,
            gpu_count=65,
        ),
        # negative price
        dict(
            gpu_type="H100",
            ip_address="1.1.1.1",
            port=8080,
            price_per_gpu=-0.01,
            gpu_count=1,
        ),
        # port out of range
        dict(
            gpu_type="H100",
            ip_address="1.1.1.1",
            port=70000,
            price_per_gpu=1.0,
            gpu_count=1,
        ),
        # empty gpu_type
        dict(
            gpu_type="", ip_address="1.1.1.1", port=8080, price_per_gpu=1.0, gpu_count=1
        ),
    ],
)
def test_add_node_rejects_invalid_payload(client, kwargs) -> None:
    portal = _Portal(post_body={})
    c = client(portal)
    with pytest.raises(Exception):
        c.add_node(**kwargs)
    assert portal.posts == []


def test_delete_node(client) -> None:
    portal = _Portal(delete_body={"message": "ok"})
    c = client(portal)
    c.delete_node("e-1")
    assert portal.deletes == [("/executors/e-1", True)]


def test_update_node_price(client) -> None:
    portal = _Portal(post_body={"message": "ok"})
    c = client(portal)
    c.update_node_price("e-1", 1.99)
    assert portal.posts[0][0] == "/executors/e-1/update-price"
    assert portal.posts[0][1] == {"price_per_gpu": 1.99}


def test_update_node_gpu(client) -> None:
    portal = _Portal(post_body={"data": {}})
    c = client(portal)
    c.update_node_gpu("e-1", gpu_type="A100", gpu_count=8)
    assert portal.posts[0][0] == "/executors/e-1/update-gpu"
    assert portal.posts[0][1] == {"gpu_type": "A100", "gpu_count": 8}


def test_set_min_gpu_for_rental(client) -> None:
    portal = _Portal(post_body={"data": {}})
    c = client(portal)
    c.set_min_gpu_for_rental("e-1", 2)
    assert portal.posts[0][0] == "/executors/e-1/min-gpu-count-for-rental"
    assert portal.posts[0][1] == {"min_gpu_count_for_rental": 2}


def test_unset_min_gpu_for_rental(client) -> None:
    portal = _Portal(delete_body={})
    c = client(portal)
    c.unset_min_gpu_for_rental("e-1")
    assert portal.deletes == [("/executors/e-1/min-gpu-count-for-rental", True)]


def test_node_pods(client) -> None:
    portal = _Portal(get_body=[{"id": "p-1"}])
    c = client(portal)
    c.node_pods("e-1")
    assert portal.gets[0][0] == "/executors/e-1/pods"


def test_create_notice_period(client) -> None:
    portal = _Portal(post_body={})
    c = client(portal)
    c.create_notice_period("e-1")
    assert portal.posts[0][0] == "/executors/e-1/notice-period"


def test_delete_notice_period(client) -> None:
    portal = _Portal(delete_body={})
    c = client(portal)
    c.delete_notice_period("e-1")
    assert portal.deletes == [("/executors/e-1/notice-period", True)]


def test_notify_machine_added(client) -> None:
    portal = _Portal(post_body={})
    c = client(portal)
    c.notify_machine_added("e-1", "r-9")
    assert portal.posts[0][0] == "/executors/e-1/machine-added"
    assert portal.posts[0][1] == {"machine_request_id": "r-9"}


# ---------------------------------------------------------------------------
# Batch sync
#
# SDK method names match the user-facing CLI verbs. ``sync_nodes_from_
# miner_server`` POSTs ``/executors/sync-executor-central-miner``, which
# is the route the "Sync From Miner Server" button hits in the frontend.
# Mapping is documented at the top of ``client.py``.


def test_sync_nodes_from_miner_server_posts_central_miner_route(client) -> None:
    portal = _Portal(post_body={})
    c = client(portal)
    c.sync_nodes_from_miner_server()
    assert portal.posts[0][0] == "/executors/sync-executor-central-miner"


def test_sync_nodes_to_miner_server_posts_miner_portal_route(client) -> None:
    portal = _Portal(post_body={})
    c = client(portal)
    c.sync_nodes_to_miner_server()
    assert portal.posts[0][0] == "/executors/sync-executor-miner-portal"


# ---------------------------------------------------------------------------
# Read-only queries


def test_billing_history_path_form(client) -> None:
    portal = _Portal(get_body={"data": []})
    c = client(portal)
    c.billing_history(miner_hotkey="5Foo")
    assert portal.gets[0][0] == "/billing/5Foo"


def test_billing_history_query_form(client) -> None:
    portal = _Portal(get_body={"data": []})
    c = client(portal)
    c.billing_history(miner_hotkey="5Foo", page=1, limit=10)
    assert portal.gets[0][0] == "/billing"
    assert portal.gets[0][1] == {"miner_hotkey": "5Foo", "page": 1, "limit": 10}


def test_list_machine_requests_and_get(client) -> None:
    portal = _Portal(get_body=[{"id": "r-1"}])
    c = client(portal)
    c.list_machine_requests()
    c.get_machine_request("r-1")
    assert [g[0] for g in portal.gets] == [
        "/machine-requests",
        "/machine-requests/r-1",
    ]


def test_list_machines(client) -> None:
    portal = _Portal(get_body=[{"name": "H100"}])
    c = client(portal)
    c.list_machines()
    assert portal.gets[0][0] == "/machines"


def test_estimated_rewards_passes_params(client) -> None:
    portal = _Portal(get_body={"rewards_on_subnet": 0})
    c = client(portal)
    c.estimated_rewards(gpu_type="H100", gpu_count=8, gpu_price=2.5)
    assert portal.gets[0][0] == "/machines/estimated-rewards"
    assert portal.gets[0][1] == {
        "gpu_type": "H100",
        "gpu_count": 8,
        "gpu_price": 2.5,
    }


# ---------------------------------------------------------------------------
# Path-segment validation


@pytest.mark.parametrize(
    "method_name,args",
    [
        ("get_node", ("../auth/me",)),
        ("delete_node", ("../auth/me",)),
        ("update_node_price", ("e/1", 1.0)),
        ("node_pods", ("..",)),
        ("get_machine_request", ("a?b",)),
    ],
)
def test_path_segment_validators_reject_unsafe_inputs(
    client, method_name, args
) -> None:
    portal = _Portal(get_body={}, post_body={}, delete_body={})
    c = client(portal)
    method = getattr(c, method_name)
    with pytest.raises(ProviderError) as exc_info:
        method(*args)
    assert exc_info.value.code == "ARG_INVALID"
    assert portal.gets == [] and portal.posts == [] and portal.deletes == []


