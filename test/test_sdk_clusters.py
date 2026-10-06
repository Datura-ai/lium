"""SDK cluster surface: list fabrics, rent N nodes as one cluster, inspect, wait, remove.

The API has had cluster endpoints since DAH-2620/DAH-2664 (`GET /executors/infiniband-clusters`,
`POST /executors/cluster/rent`, `cluster_id`/`cluster_node_index`/`cluster_overlay_ip` on every
pod); the SDK could not reach any of it, so a multi-node job could only be started from the web UI.
"""

import itertools
import time
from types import SimpleNamespace

import pytest

from lium.sdk import (
    Cluster, ClusterNotListedError, ClusterOffer, Config, Lium, LiumError, LiumNotFoundError, LiumPermissionError, Template,
)
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


def test_up_cluster_by_name_ignores_a_cluster_that_existed_before_the_call(monkeypatch):
    """An older cluster reusing the pod name is not the one this rental produced."""
    stale = _pods([_pod_payload(7, cluster_id="c-old"), _pod_payload(8, cluster_id="c-old")])
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): LiumServerError("Server error: 504")},
        ps_sequence=[stale, stale],
    )

    with pytest.raises(LiumError, match="did not produce pods named 'job'"):
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")


def test_up_cluster_by_name_takes_the_new_cluster_next_to_a_stale_one(monkeypatch):
    stale = _pods([_pod_payload(7, cluster_id="c-old"), _pod_payload(8, cluster_id="c-old")])
    fresh = _pods([_pod_payload(0), _pod_payload(1)])
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): LiumServerError("Server error: 504")},
        ps_sequence=[stale, stale + fresh],
    )

    cluster = client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert cluster.id == "c-1" and [p.id for p in cluster.pods] == ["pod-0", "pod-1"]


def test_up_cluster_reports_when_nothing_appeared(monkeypatch):
    client = _Client(routes={("POST", "/executors/cluster/rent"): LiumServerError("Server error: 504")}, ps_sequence=[[]])

    with pytest.raises(LiumError, match="did not produce pods named 'job'"):
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")


def test_up_cluster_waits_until_every_named_member_is_listed(monkeypatch):
    """The rent route names two pods; the listing shows one of them at first. Returning then hands back a
    one-member cluster — a --ttl would skip the second node and a wait would not wait for it (it kept billing
    unscheduled). The lookup must keep polling until both are listed."""
    both = _pods([_pod_payload(0), _pod_payload(1)])
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): {"success": True, "pod_ids": ["pod-0", "pod-1"]}},
        ps_sequence=[[], _pods([_pod_payload(0)]), both],   # before the rent; then one member listed; then both
    )

    cluster = client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert [p.id for p in cluster.pods] == ["pod-0", "pod-1"]
    assert sum(1 for c in client.calls if c[0] == "ps") == 3   # the pre-rent snapshot, the partial listing, the full one


def test_up_cluster_confirmed_but_not_listed_is_not_a_nothing_happened_error(monkeypatch):
    """The API confirmed the rent and named the pods, but the listing never showed the second one: the nodes are
    rented and billing, so the error must not read like the 'nothing appeared' case a caller would answer with a
    second rental. It names the confirmed ids and what the listing showed."""
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): {"success": True, "pod_ids": ["pod-0", "pod-1"]}},
        ps_sequence=[_pods([_pod_payload(0)])],
    )

    with pytest.raises(ClusterNotListedError, match="shows 1 of 2 members") as info:
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert info.value.pod_ids == ["pod-0", "pod-1"] and info.value.listed == ["pod-0"]
    assert "did not produce" not in str(info.value)
    assert sum(1 for c in client.calls if c[0] == "POST") == 1


def test_up_cluster_confirmed_but_listing_fails_is_not_a_retry_error(monkeypatch):
    """The API confirmed the rent and named the pods, then every listing failed. The old code let the listing's
    exception out as a plain server error, whose CLI hint is 'Retry': a retry rents a second cluster. The nodes
    are rented, so this is the confirmed-but-not-listed case."""
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): {"success": True, "pod_ids": ["pod-0", "pod-1"]}},
        ps_sequence=[[], LiumServerError("Server error: 503")],   # the pre-rent snapshot works; every listing after fails
    )

    with pytest.raises(ClusterNotListedError, match="listing failed \\(Server error: 503\\)") as info:
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert info.value.confirmed is True and info.value.pod_ids == ["pod-0", "pod-1"] and info.value.listed == []
    assert "rented and billing" in str(info.value)
    assert sum(1 for c in client.calls if c[0] == "POST") == 1
    assert sum(1 for c in client.calls if c[0] == "ps") == 4   # the snapshot, then three failed attempts


def test_up_cluster_aborts_before_the_order_when_the_baseline_listing_fails(monkeypatch):
    """The pre-rent snapshot is what keeps the by-name recovery from handing back an older cluster of the same
    name. The old code treated a failed snapshot as 'no clusters', so a timeout after it could return the older
    cluster. Nothing is rented before the order, so the listing error is let out and the order is not sent."""
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): {"success": True, "pod_ids": ["pod-0", "pod-1"]}},
        ps_sequence=[LiumServerError("Server error: 503")],
    )

    with pytest.raises(LiumServerError, match="503"):
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert not any(c[0] == "POST" for c in client.calls)


def test_up_cluster_by_name_short_of_the_requested_count_is_an_uncertain_rent(monkeypatch):
    """The order got no answer and the by-name lookup finds a new cluster of that name with one of the two
    members. The old code returned it: a --ttl skipped the second node and a wait did not wait for it. The
    rent may well have gone through in full, so the error says the nodes may be rented and never 'Retry'."""
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): LiumServerError("Server error: 504")},
        ps_sequence=[[], _pods([_pod_payload(0)])],
    )

    with pytest.raises(ClusterNotListedError, match="shows 1 new pod\\(s\\) named 'job' where 2 members were requested") as info:
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert info.value.confirmed is False and info.value.pod_ids == [] and info.value.listed == ["pod-0"]
    assert "may be rented" in str(info.value) and "did not produce" not in str(info.value)
    assert sum(1 for c in client.calls if c[0] == "POST") == 1


def test_up_cluster_by_name_waits_for_the_full_count(monkeypatch):
    """Same start, but the second member is listed on a later attempt: the whole cluster comes back."""
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): LiumServerError("Server error: 504")},
        ps_sequence=[[], _pods([_pod_payload(0)]), _pods([_pod_payload(0), _pod_payload(1)])],
    )

    cluster = client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert [p.id for p in cluster.pods] == ["pod-0", "pod-1"]


def test_up_cluster_no_answer_and_no_listing_is_an_uncertain_rent(monkeypatch):
    """The order got no answer and no listing answered either: nothing says whether the nodes are rented, so the
    error must not read like 'nothing appeared' (which a caller answers with a second rental)."""
    client = _Client(
        routes={("POST", "/executors/cluster/rent"): LiumServerError("Server error: 504")},
        ps_sequence=[[], LiumServerError("Server error: 503")],
    )

    with pytest.raises(ClusterNotListedError, match="listing failed") as info:
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert info.value.confirmed is False and info.value.listed == []
    assert "may be rented" in str(info.value)


def test_up_cluster_refusal_is_reported_without_polling(monkeypatch):
    """A 200 with success=false is a definite refusal: nothing was rented, so no listing is consulted."""
    client = _Client(routes={("POST", "/executors/cluster/rent"): {"success": False, "message": "mixed fabrics"}})

    with pytest.raises(LiumError, match="mixed fabrics"):
        client.up_cluster(["exec-0", "exec-1"], name="job", template_id="tpl-x")

    assert sum(1 for c in client.calls if c[0] == "ps") == 1   # only the pre-rent snapshot


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


def test_wait_cluster_ready_stops_at_once_when_a_member_dies(monkeypatch):
    """A FAILED member is reported on the poll that sees it, not at the deadline: N nodes keep billing meanwhile."""
    from lium.sdk import PodStartError

    dead = _pods([_pod_payload(0), _pod_payload(1, status="FAILED")])
    client = _Client(ps_sequence=[dead, dead, dead])
    monkeypatch.setattr(time, "time", lambda: 0.0)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    with pytest.raises(PodStartError, match="job is FAILED") as raised:
        client.wait_cluster_ready(Cluster(id="c-1", pods=dead), timeout=900)

    assert raised.value.status == "FAILED" and raised.value.pod.id == "pod-1"


def test_wait_cluster_ready_stops_when_a_member_vanishes(monkeypatch):
    """A member that was listed and then disappears is a dead member, not a slow one."""
    from lium.sdk import PodStartError

    both = _pods([_pod_payload(0), _pod_payload(1, status="PENDING")])
    one = _pods([_pod_payload(0)])
    client = _Client(ps_sequence=[both, one, one])
    monkeypatch.setattr(time, "time", lambda: 0.0)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    with pytest.raises(PodStartError, match="1 member\\(s\\) vanished"):
        client.wait_cluster_ready(Cluster(id="c-1", pods=both), timeout=900)


def test_wait_cluster_ready_stops_when_a_member_is_missing_on_the_first_poll(monkeypatch):
    """The members waited for are the ones the caller gave. The old code only counted members after a poll had
    shown the full set, so a member gone on the first poll was waited for until the deadline (15 minutes by
    default) while the rest billed. It is reported on that first poll, by id."""
    from lium.sdk import PodStartError

    both = _pods([_pod_payload(0), _pod_payload(1, status="PENDING")])
    one = _pods([_pod_payload(0)])
    client = _Client(ps_sequence=[one])
    clock = itertools.count(0.0, 500.0)   # advances, so the old code fails with TimeoutError instead of hanging
    monkeypatch.setattr(time, "time", lambda: next(clock))

    with pytest.raises(PodStartError, match="1 member\\(s\\) vanished from the pod list \\(pod-1\\)"):
        client.wait_cluster_ready(Cluster(id="c-1", pods=both), timeout=900)

    assert sum(1 for c in client.calls if c[0] == "ps") == 1


def test_wait_cluster_ready_outlives_a_failing_listing_until_the_deadline(monkeypatch):
    """A listing that fails is not a member that failed. The old code let the listing's error out of the wait,
    and the CLI's generic hint for it is 'Retry' while the cluster bills. The poll goes on; if the listing never
    answers, the deadline reports it as a timeout that names the listing failure."""
    pending = _pods([_pod_payload(0), _pod_payload(1, status="PENDING")])
    ready = _pods([_pod_payload(0), _pod_payload(1)])
    client = _Client(ps_sequence=[LiumServerError("Server error: 503"), ready])
    monkeypatch.setattr(time, "time", lambda: 0.0)

    cluster = client.wait_cluster_ready(Cluster(id="c-1", pods=pending), timeout=900)

    assert cluster.status == "RUNNING" and sum(1 for c in client.calls if c[0] == "ps") == 2

    client = _Client(ps_sequence=[LiumServerError("Server error: 503")])
    clock = iter([0.0, 0.0, 1000.0])
    monkeypatch.setattr(time, "time", lambda: next(clock))

    with pytest.raises(TimeoutError, match="the pod listing failed \\(Server error: 503\\)"):
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


# `rm_cluster` is one HTTP call, so these run the real `_request` against a scripted `requests.request`
# (as test_sdk_error_context does): the status mapping in `_raise_for_status` is part of what is tested.

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


def test_rm_cluster_is_one_delete_by_cluster_id_and_reports_every_member(monkeypatch, tmp_path):
    """Regression: the old code deleted each member with `DELETE /pods/{id}` in a loop after two `GET /pods`; a
    member the listing had not shown yet kept billing. The server now owns the group (lium-platform#411)."""
    pods = _pods([_pod_payload(0), _pod_payload(1)])
    body = {"cluster_id": "c-1", "success": True, "message": "Cluster deletion started for every member",
            "pods": [_member_row(0), _member_row(1, message="Pod deletion is in progress")]}
    client = _http_client(monkeypatch, tmp_path, _HttpResponse(200, body))
    for pod in pods:  # a pinned host key per member, as an ssh session leaves behind
        path = tmp_path / ".lium" / "known_hosts" / pod.id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("key")

    results = client.rm_cluster(Cluster(id="c-1", pods=pods))

    assert client.calls == [("DELETE", f"{client.config.base_url}/clusters/c-1")]
    assert results == [
        {"pod": "pod-0", "huid": pods[0].huid, "name": "job", "node_rank": 0, "success": True, "message": "Pod deletion started", "error": None},
        {"pod": "pod-1", "huid": pods[1].huid, "name": "job", "node_rank": 1, "success": True, "message": "Pod deletion is in progress", "error": None},
    ]
    assert not list((tmp_path / ".lium" / "known_hosts").iterdir())  # both host keys dropped, as rm() does


def test_rm_cluster_reports_the_member_that_failed_and_keeps_the_rest(monkeypatch, tmp_path):
    """The server removes the other members when one fails and says so per row; the failed row's message is the
    error the caller shows, and only the accepted member's host key is dropped."""
    pods = _pods([_pod_payload(0), _pod_payload(1)])
    body = {"cluster_id": "c-1", "success": False,
            "message": "Cluster deletion failed for 1 of 2 member(s); the others are being removed",
            "pods": [_member_row(0), _member_row(1, success=False, message="Pod deletion failed; call again to retry this member, or delete the pod on its own")]}
    client = _http_client(monkeypatch, tmp_path, _HttpResponse(200, body))
    hosts = tmp_path / ".lium" / "known_hosts"
    hosts.mkdir(parents=True)
    (hosts / "pod-0").write_text("key")
    (hosts / "pod-1").write_text("key")

    results = client.rm_cluster(Cluster(id="c-1", pods=pods))

    assert [(r["pod"], r["success"]) for r in results] == [("pod-0", True), ("pod-1", False)]
    assert results[1]["error"].startswith("Pod deletion failed; call again")
    assert sorted(p.name for p in hosts.iterdir()) == ["pod-1"]


def test_rm_cluster_404_is_raised_and_nothing_falls_back_to_per_pod_deletes(monkeypatch, tmp_path):
    """A 404 from the route (the cluster is gone, or the server has no `DELETE /clusters/{id}`) is
    `LiumNotFoundError` with the server's text. Exactly one request goes out: no `GET /pods`, no `DELETE /pods/{id}`."""
    pods = _pods([_pod_payload(0), _pod_payload(1)])
    client = _http_client(monkeypatch, tmp_path, _HttpResponse(404, _error_body(404, "Cluster not found")))

    with pytest.raises(LiumNotFoundError, match="Cluster not found"):
        client.rm_cluster(Cluster(id="c-1", pods=pods))

    assert client.calls == [("DELETE", f"{client.config.base_url}/clusters/c-1")]


def test_rm_cluster_409_and_an_answer_without_rows_are_errors_not_removals(monkeypatch, tmp_path):
    """409: a member is still being created and the platform does not cancel in-flight creates; the server removed
    nothing. A 200 without `pods` rows (not a shape #411 sends) must not come back as an empty, all-good result either:
    `clusters rm` would exit 0 on a cluster that is still billing."""
    pods = _pods([_pod_payload(0), _pod_payload(1)])
    detail = {"message": "A member of this cluster is still being created; try again later.", "cluster_id": "c-1", "pending_pods": ["pod-1"]}
    conflict = _http_client(monkeypatch, tmp_path, _HttpResponse(409, {**_error_body(409, "conflict"), "message": detail}))
    with pytest.raises(LiumError, match="API error 409") as raised:
        conflict.rm_cluster(Cluster(id="c-1", pods=pods))
    assert "still being created" in str(raised.value) and not isinstance(raised.value, (LiumNotFoundError, LiumPermissionError))

    empty = _http_client(monkeypatch, tmp_path, _HttpResponse(200, {"cluster_id": "c-1", "success": True, "message": "ok", "pods": []}))
    with pytest.raises(LiumError, match="without per-member results"):
        empty.rm_cluster(Cluster(id="c-1", pods=pods))


def test_rm_cluster_403_is_a_permission_error(monkeypatch, tmp_path):
    """A member of another user, or a key without the manage scope: the server refuses before the first delete."""
    pods = _pods([_pod_payload(0)])
    client = _http_client(monkeypatch, tmp_path, _HttpResponse(403, _error_body(403, "You do not have permission to access this pod")))

    with pytest.raises(LiumPermissionError, match="Permission denied: You do not have permission"):
        client.rm_cluster(Cluster(id="c-1", pods=pods))

    assert len(client.calls) == 1
