"""What the active pods cost: per pod, per hour, and how long the balance lasts.

The platform bills a pod its own hourly price while it is RUNNING (or in a
reboot state) and nothing while it is PENDING; the API reports the price and
`created_at` but neither what it has billed nor when RUNNING began. So "spent"
is price × wall time since creation for a pod that is billing — an estimate
that counts the start-up minutes and ignores restarts and price changes — and
$0 for a PENDING pod, which has not started billing. `lium up --budget` and
`lium rm` count from the same `created_at` and `rm` applies the same PENDING
rule; `ps`'s Spent column is main's price × age for every status (unchanged here).
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich.table import Table

from lium.sdk import PodInfo
from lium.cli.ps.display import _gpu_config, _parse_timestamp

ESTIMATE_NOTE = (
    "spent is price × wall time since creation (it counts the start-up minutes; the API does not report billed "
    "spend or when RUNNING began) and $0 for a PENDING pod, which is not billing yet. "
    "burn counts RUNNING/REBOOT_PENDING/REBOOT_FAILED pods only (what the platform bills) and excludes volume storage"
)
# The platform bills a pod in these statuses (a reboot keeps the reservation);
# a PENDING or FAILED pod is not charged now, so it is not part of the burn.
BILLABLE_STATUSES = frozenset({"RUNNING", "REBOOT_PENDING", "REBOOT_FAILED"})
# A pod that has not left PENDING has not billed a cent: its spend is $0, not price × age.
NOT_YET_BILLING = frozenset({"PENDING"})


@dataclass
class PodSpend:
    huid: str
    name: Optional[str]
    status: Optional[str]
    config: Optional[str]
    price_per_hour: Optional[float]
    since: Optional[str]
    uptime_hours: Optional[float]
    spent_usd: Optional[float]
    billable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def pod_spend(pod: PodInfo, now: Optional[datetime] = None) -> PodSpend:
    now = now or datetime.now(timezone.utc)
    executor = pod.executor
    price = executor.price_per_hour if executor and executor.price_per_hour is not None else None
    created = _parse_timestamp(pod.created_at) if pod.created_at else None
    hours = round((now - created).total_seconds() / 3600, 3) if created else None
    config = _gpu_config(pod)  # the pod's own GPU count, as `ps` labels it — a split rental is not the whole host
    status = pod.status.upper() if pod.status else None
    if status in NOT_YET_BILLING:
        spent: Optional[float] = 0.0
    else:
        spent = round(hours * price, 2) if hours is not None and price is not None else None
    return PodSpend(
        huid=pod.huid,
        name=pod.name,
        status=status,
        config=config,
        price_per_hour=price,
        since=created.isoformat(timespec="seconds") if created else None,
        uptime_hours=hours,
        spent_usd=spent,
        billable=status in BILLABLE_STATUSES,
    )


@dataclass
class SpendReport:
    pods: List[PodSpend]
    burn_per_hour: float
    spent_usd: float
    balance_usd: Optional[float]
    runway_hours: Optional[float]
    note: str = ESTIMATE_NOTE

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["pods"] = [p.to_dict() for p in self.pods]
        return data


def build_report(pods: List[PodInfo], balance: Optional[float], now: Optional[datetime] = None) -> SpendReport:
    rows = sorted((pod_spend(p, now) for p in pods), key=lambda r: -(r.spent_usd or 0))
    burn = round(sum(r.price_per_hour or 0 for r in rows if r.billable), 4)
    spent = round(sum(r.spent_usd or 0 for r in rows), 2)
    runway = round(balance / burn, 1) if balance is not None and burn > 0 and balance > 0 else None
    if balance is not None and burn > 0 and balance <= 0:
        runway = 0.0
    return SpendReport(pods=rows, burn_per_hour=burn, spent_usd=spent, balance_usd=balance, runway_hours=runway)


def _hours(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value < 1:
        return f"{value * 60:.0f}m"
    if value < 48:
        return f"{value:.1f}h"
    return f"{value / 24:.1f}d"


def _usd(value: Optional[float]) -> str:
    return "-" if value is None else f"${value:,.2f}"


def build_table(report: SpendReport) -> Table:
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, padding=(0, 1))
    table.add_column("Pod", no_wrap=True)
    table.add_column("Config", no_wrap=True)
    table.add_column("$/h", justify="right", no_wrap=True)
    table.add_column("Uptime", justify="right", no_wrap=True)
    table.add_column("Spent", justify="right", no_wrap=True)
    table.add_column("Since (UTC)", no_wrap=True)
    for row in report.pods:
        since = row.since.replace("T", " ")[:16] if row.since else "-"
        table.add_row(row.name or row.huid, row.config or "-", _usd(row.price_per_hour), _hours(row.uptime_hours), _usd(row.spent_usd), since)
    return table


def summary_lines(report: SpendReport) -> List[str]:
    pods = len(report.pods)
    billable = sum(1 for r in report.pods if r.billable)
    counted = f"{billable} of {pods} pods" if billable != pods else f"{pods} pod{'s' if pods != 1 else ''}"
    lines = [
        f"Burn {_usd(report.burn_per_hour)}/h across {counted} (volumes not included); "
        f"spent so far {_usd(report.spent_usd)} (estimated)",
    ]
    if report.balance_usd is not None:
        runway = "-" if report.runway_hours is None else _hours(report.runway_hours)
        lines.append(f"Balance {_usd(report.balance_usd)}; runway at this burn ~{runway}")
    return lines
