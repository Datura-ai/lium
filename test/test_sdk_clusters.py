"""SDK cluster surface: list fabrics, rent N nodes as one cluster, inspect, wait, remove.

The API has had cluster endpoints since DAH-2620/DAH-2664 (`GET /executors/infiniband-clusters`,
`POST /executors/cluster/rent`, `cluster_id`/`cluster_node_index`/`cluster_overlay_ip` on every
pod); the SDK could not reach any of it, so a multi-node job could only be started from the web UI.
"""

import time
from types import SimpleNamespace

import pytest

from lium.sdk import Cluster, ClusterOffer, Config, Lium, LiumError, LiumNotFoundError, Template
from lium.sdk.exceptions import LiumServerError

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
        self.removed: list = []

    def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs.get("json")))
        answer = self.routes.get((method, endpoint))
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer()
        return SimpleNamespace(json=lambda: answer)

    def ps(self):
        self.calls.append(("ps",))
        if not self._ps_sequence:
            return []
        if len(self._ps_sequence) > 1:
            return self._ps_sequence.pop(0)
        return self._ps_sequence[0]

    def _ensure_ssh_keys_registered(self, public_keys, name=None):
        self.calls.append(("ssh-keys", tuple(public_keys)))

    def rm(self, pod):
        self.removed.append(pod.id)
        if pod.id in getattr(self, "rm_fails", ()):
            raise LiumError("boom")
        return {"ok": True}


def _pods(payloads):
    """Run payloads through the real ps() parser once, so the cluster fields are exercised."""
    c = _Client(routes={("GET", "/pods"): payloads})
    return Lium.ps(c)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


# --- parsing --------------------------------------------------------------------------------------

def test_ps_reads_the_cluster_fields_of_a_member_pod():
    pods = _pods([_pod_payload(0), {**_pod_payload(9), "cluster_id": None, "cluster_node_index": None, "cluster_overlay_ip": None}])

    assert (pods[0].cluster_id, pods[0].cluster_node_index, pods[0].cluster_overlay_ip) == ("c-1", 0, "10.42.0.1")
    assert (pods[1].cluster_id, pods[1].cluster_node_index, pods[1].cluster_overlay_ip) == (None, None, None)


def test_clusters_lists_offers_with_their_free_nodes():
    client = _Client(routes={("GET", "/executors/infiniband-clusters"): [_offer_payload()]})

    offers = client.clusters()

    assert len(offers) == 1
    offer = offers[0]
    assert isinstance(offer, ClusterOffer)
    assert (offer.fabric_id, offer.fabric_type, offer.link_rate, offer.fabric_measured) == (FABRIC, "infiniband", "400 Gb/sec (4X NDR)", True)
    assert (offer.node_count, offer.free_count, offer.gpu_type, offer.gpus_per_node) == (4, 3, "H100", 8)
    assert offer.price_per_node_hour == 16.0  # 8 GPUs × $2.0 on the cheapest node


def test_offer_cheapest_picks_by_price_and_refuses_too_many():
    offer = _Client(routes={("GET", "/executors/infiniband-clusters"): [_offer_payload()]}).clusters()[0]

    assert [n.id for n in offer.cheapest(2)] == ["exec-0", "exec-1"]
    with pytest.raises(ValueError, match="Only 3 of 4 nodes"):
        offer.cheapest(4)
    with pytest.raises(ValueError):
        offer.cheapest(0)


def test_cluster_offer_resolves_exact_id_or_unique_prefix():
    other = {**_offer_payload(), "fabric_id": "cold:roce:10.0.0.0/24:NVIDIA GeForce RTX 4090:8"}
    client = _Client(routes={("GET", "/executors/infiniband-clusters"): [_offer_payload(), other]})

    assert client.cluster_offer(FABRIC).fabric_id == FABRIC
    assert client.cluster_offer("cold:").fabric_id.startswith("cold:")
    assert client.cluster_offer("nope") is None


# --- Cluster model --------------------------------------------------------------------------------

def test_cluster_orders_members_by_rank_and_derives_launcher_material():
    pods = _pods([_pod_payload(2), _pod_payload(0), _pod_payload(1)])

    cluster = Cluster(id="c-1", pods=pods)

    assert [p.cluster_node_index for p in cluster.pods] == [0, 1, 2]
    assert cluster.size == 3 and cluster.master.id == "pod-0" and cluster.master_addr == "10.42.0.1"
    assert cluster.status == "RUNNING" and cluster.price_per_hour == 48.0
    assert cluster.hostfile() == "10.42.0.1 slots=8\n10.42.0.2 slots=8\n10.42.0.3 slots=8\n"
    assert cluster.hostfile(slots=4).splitlines()[0] == "10.42.0.1 slots=4"
    assert cluster.node_rank(pods[0]) == 2
    assert cluster.torchrun_args(pods[0]) == "--nnodes 3 --node_rank 2 --master_addr 10.42.0.1 --master_port 29500"
    with pytest.raises(ValueError, match="not a member"):
        cluster.node_rank(_pods([_pod_payload(7, cluster_id="other")])[0])


def test_cluster_status_names_the_member_that_is_not_running():
    pods = _pods([_pod_payload(0), _pod_payload(1, status="PENDING")])

    assert Cluster(id="c-1", pods=pods).status == "PENDING"
    assert Cluster(id="c-1", pods=[]).status == "EMPTY"


# --- cluster_template -----------------------------------------------------------------------------

def _template(tag: str, image: str = "daturaai/lium-cluster") -> Template:
    return Template(id=f"tpl-{tag}", name="Multi-node cluster", huid="h", docker_image=image, docker_image_tag=tag,
                    category="PYTORCH", status="VERIFY_SUCCESS")


def test_cluster_template_prefers_the_newest_cluster_image(monkeypatch):
    client = _Client()
    monkeypatch.setattr(client, "templates", lambda *a, **k: [
        _template("0.0.7"), _template("0.0.10"), _template("2.1", image="daturaai/pytorch"),
    ])

    assert client.cluster_template().id == "tpl-0.0.10"


def test_cluster_template_missing_is_an_error(monkeypatch):
    client = _Client()
    monkeypatch.setattr(client, "templates", lambda *a, **k: [_template("2.1", image="daturaai/pytorch")])

    with pytest.raises(LiumNotFoundError, match="daturaai/lium-cluster"):
        client.cluster_template()


# --- up_cluster -----------------------------------------------------------------------------------

def test_up_cluster_posts_the_group_and_returns_the_members(monkeypatch):
    members = [_pod_payload(0), _pod_payload(1)]
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): {"success": True, "pod_ids": ["pod-0", "pod-1"]}},
        ps_sequence=[_pods(members + [_pod_payload(5, cluster_id="other")])],
    )
    monkeypatch.setattr(client, "cluster_template", lambda: _template("0.0.7"))

    cluster = client.up_cluster(["exec-0", "exec-1"], name="job", ports=2)

    post = next(c for c in client.calls if c[0] == "POST")
    assert post[1] == "/executors/cluster/rent"
    assert post[2] == {
        "pod_name": "job", "template_id": "tpl-0.0.7", "executor_uuids": ["exec-0", "exec-1"],
        "user_public_key": ["ssh-ed25519 AAAA test"], "initial_port_count": 2, "enable_volume_encryption": True,
    }
    assert ("ssh-keys", ("ssh-ed25519 AAAA test",)) in client.calls
    assert cluster.id == "c-1" and [p.id for p in cluster.pods] == ["pod-0", "pod-1"]


def test_up_cluster_uses_the_given_template_and_skips_the_lookup(monkeypatch):
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): {"success": True, "pod_ids": ["pod-0", "pod-1"]}},
        ps_sequence=[_pods([_pod_payload(0), _pod_payload(1)])],
    )
    monkeypatch.setattr(client, "cluster_template", lambda: pytest.fail("should not look up the template"))

    client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert next(c for c in client.calls if c[0] == "POST")[2]["template_id"] == "tpl-x"


def test_up_cluster_rejects_bad_input_before_calling_the_api():
    client = _Client()
    with pytest.raises(ValueError, match="at least two"):
        client.up_cluster(["exec-0"], name="job")
    with pytest.raises(ValueError, match="duplicates"):
        client.up_cluster(["exec-0", "exec-0"], name="job")
    client.config.keys = []
    with pytest.raises(ValueError, match="No SSH keys"):
        client.up_cluster(["exec-0", "exec-1"], name="job")
    assert client.calls == []


def test_up_cluster_surfaces_the_api_refusal(monkeypatch):
    client = _Client(routes={("POST", "/executors/cluster/rent"): {"success": False, "message": "A cluster rental must take whole nodes"}})

    with pytest.raises(LiumError, match="whole nodes"):
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")


def test_up_cluster_does_not_resend_after_a_server_error_but_finds_the_pods_by_name(monkeypatch):
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): LiumServerError("Server error: 504")},
        ps_sequence=[[], _pods([_pod_payload(0), _pod_payload(1)])],
    )

    cluster = client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert cluster.id == "c-1"
    assert sum(1 for c in client.calls if c[0] == "POST") == 1


def test_up_cluster_reports_when_nothing_appeared(monkeypatch):
    client = _Client(routes={("POST", "/executors/cluster/rent"): LiumServerError("Server error: 504")}, ps_sequence=[[]])

    with pytest.raises(LiumError, match="did not produce pods named 'job'"):
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")


def test_up_cluster_with_wait_returns_the_ready_cluster(monkeypatch):
    pending = _pods([_pod_payload(0), _pod_payload(1, status="PENDING")])
    ready = _pods([_pod_payload(0), _pod_payload(1)])
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): {"success": True, "pod_ids": ["pod-0", "pod-1"]}},
        ps_sequence=[pending, pending, ready],
    )

    cluster = client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x", wait=True, timeout=100)

    assert cluster.status == "RUNNING" and all(p.ssh_cmd for p in cluster.pods)


def test_wait_cluster_ready_times_out_naming_the_pending_member(monkeypatch):
    pending = _pods([_pod_payload(0), _pod_payload(1, status="PENDING")])
    client = _Client(ps_sequence=[pending])
    clock = iter([0.0, 0.0, 1000.0])
    monkeypatch.setattr(time, "time", lambda: next(clock))

    with pytest.raises(TimeoutError, match="job=PENDING"):
        client.wait_cluster_ready(Cluster(id="c-1", pods=pending), timeout=10)


# --- my_clusters / cluster / rm_cluster -----------------------------------------------------------

def test_my_clusters_groups_pods_by_cluster_id():
    pods = _pods([_pod_payload(0), _pod_payload(1), _pod_payload(0, cluster_id="c-2"), {**_pod_payload(9), "cluster_id": None}])
    client = _Client(ps_sequence=[pods])

    clusters = {c.id: c.size for c in client.my_clusters()}

    assert clusters == {"c-1": 2, "c-2": 1}


def test_cluster_by_id_or_unique_prefix():
    pods = _pods([_pod_payload(0, cluster_id="abc-111"), _pod_payload(0, cluster_id="abd-222")])
    client = _Client(ps_sequence=[pods])

    assert client.cluster("abc-111").id == "abc-111"
    assert client.cluster("abd").id == "abd-222"
    with pytest.raises(LiumNotFoundError, match="ambiguous"):
        client.cluster("ab")
    with pytest.raises(LiumNotFoundError):
        client.cluster("zzz")


def test_rm_cluster_removes_every_member_and_keeps_going_after_a_failure():
    pods = _pods([_pod_payload(0), _pod_payload(1), _pod_payload(2)])
    client = _Client()
    client.rm_fails = {"pod-1"}

    results = client.rm_cluster(Cluster(id="c-1", pods=pods))

    assert client.removed == ["pod-0", "pod-1", "pod-2"]
    assert [(r["pod"], r["success"]) for r in results] == [("pod-0", True), ("pod-1", False), ("pod-2", True)]
    assert results[1]["error"] == "boom"
