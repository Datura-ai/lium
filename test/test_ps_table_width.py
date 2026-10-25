"""`lium ps` stays readable at 80 columns and behind a pipe: one line per huid, ports collapsed (DAH-5148)."""

import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rich.console import Console

from lium.cli.ps import display
from lium.sdk import PodInfo


def _pod(huid: str, price: float = 0.24, gpu: str = "A4000", gpus: int = 1, created_at: str = "2026-10-25T00:00:00Z",
         removal: str | None = None) -> PodInfo:
    ports = {"22": 20301, **{str(8000 + i): 20302 + i for i in range(10)}}
    executor = SimpleNamespace(ip="203.0.113.7", price_per_hour=price, gpu_type=gpu, gpu_count=gpus, specs={}, id="e")
    return PodInfo(
        id=f"id-{huid}", name="train", huid=huid, status="RUNNING", ssh_cmd="ssh root@203.0.113.7 -p 20301",
        ports=ports, created_at=created_at, updated_at="", executor=executor,
        template={"name": "pytorch"}, removal_scheduled_at=removal, jupyter_installation_status=None, jupyter_url=None,
    )


def test_ports_collapse_to_the_ssh_mapping_and_a_count():
    assert display._format_ports({"8000": 20302, "22": 20301, "8001": 20303}) == "22:20301 +2"
    assert display._format_ports({"22": 20301}) == "22:20301"
    assert display._format_ports({}) == "—"


def test_ps_table_at_80_columns_keeps_each_huid_on_one_line():
    pods = [_pod("noble-matrix-27"), _pod("eager-wolf-aa")]
    for short in (False, True):  # piped (Ports shown) and a narrow terminal (Ports hidden)
        table, _ = display.build_pods_table(pods, short=short)
        out = io.StringIO()
        Console(file=out, width=80, force_terminal=False).print(table)
        text = out.getvalue()
        assert "noble-matrix-27" in text and "eager-wolf-aa" in text
        assert max(len(line) for line in text.splitlines()) <= 80
        assert ("22:20301 +10" in text) is not short


def test_ps_table_at_80_columns_keeps_a_capped_spend_whole():
    # 8x RTXPRO6000 at $8/h, 25 hours old, removal at 60 hours: "$200.00/$480.00"
    created = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=25)
    capped = _pod("noble-matrix-27", price=8.0, gpu="RTXPRO6000", gpus=8, created_at=created.isoformat(),
                  removal=(created + timedelta(hours=60)).isoformat())
    for pods in ([capped], [capped, _pod("eager-wolf-aa")]):
        for short in (False, True):
            table, _ = display.build_pods_table(pods, short=short)
            out = io.StringIO()
            Console(file=out, width=80, force_terminal=False).print(table)
            assert "$200.00/$480.00" in out.getvalue()
