"""Remove (rm) command implementation."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
import click

from lium.sdk import Lium, PodInfo
from lium.cli import ui
from lium.cli.workspaces.context import show_workspace
from lium.cli.utils import (
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    CliFailure,
    TargetMatch,
    handle_errors,
)
from . import validation, parsing, display
from .actions import RemovePodsAction, ScheduleRemovalAction


@dataclass(frozen=True)
class RemovalPlan:
    """Which pods to remove, and when — now if no time was given."""

    pods: List[PodInfo]
    termination_time: Optional[datetime]
    # Targets that were `lium ps` row numbers rather than names; the command
    # spells out what each one resolved to before acting on it.
    index_matches: List[TargetMatch] = field(default_factory=list)


def build_removal_plan(
    lium: Lium,
    targets: Optional[str],
    remove_all: bool,
    in_duration: Optional[str],
    at_time: Optional[str],
    allow_index: Optional[bool] = None,
    quiet: bool = False,
) -> Optional[RemovalPlan]:
    """Resolve TARGETS into a plan. None means there was nothing to remove.

    ``quiet`` keeps the "nothing to remove" note off stdout (``--format json``
    prints its own payload there).
    """
    is_valid, error = validation.validate(targets, remove_all, in_duration, at_time)
    if not is_valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    all_pods = ui.load("Loading pods", lambda: lium.ps())
    if not all_pods:
        # Removing everything from an empty account is a no-op, not a failure.
        # Naming a pod that is not there is a failure — that is a typo, and it
        # must not read like a successful teardown.
        if remove_all:
            if not quiet:
                ui.warning("No active pods")
            return None
        raise CliFailure(
            "pod_not_found", f"{parsing.NO_MATCHING_PODS}: {targets}", EXIT_POD_NOT_FOUND
        )

    parsed, error = parsing.parse(
        targets, remove_all, all_pods, in_duration, at_time, allow_index=allow_index
    )
    if error:
        raise CliFailure(
            "pod_not_found" if error.startswith(parsing.NO_MATCHING_PODS) else "invalid_arguments",
            error,
            EXIT_POD_NOT_FOUND
            if error.startswith(parsing.NO_MATCHING_PODS)
            else EXIT_CONFIGURATION_ERROR,
        )

    return RemovalPlan(
        pods=parsed["selected_pods"],
        termination_time=parsed.get("termination_time"),
        index_matches=parsed.get("index_matches", []),
    )


def describe_index_match(match: TargetMatch) -> str:
    """'1 → eager-wolf-aa (name: train)': what a row number stands for."""
    pod = match.pod
    name = f" (name: {pod.name})" if pod.name and pod.name != pod.huid else ""
    return f"{match.target} → {pod.huid}{name}"


def human_approved_index_targets(matches: List[TargetMatch], yes: bool = False, on_stderr: bool = False) -> bool:
    """Show what each row number resolved to; ask before acting when someone can answer.

    A number is the one way to name a pod the caller may never have looked at, so
    the pod behind it is spelled out here — huid and name — before anything is
    removed, with or without --yes. Without a terminal nobody can answer, so the
    command fails closed (``confirmation_required``) unless ``--yes`` was given:
    a script names its intent with the flag, never by the absence of a prompt.
    ``on_stderr`` keeps the line off stdout when stdout is a JSON document
    (``--format json``).
    """
    for match in matches:
        (ui.notice if on_stderr else ui.info)(f"Pod {describe_index_match(match)}")
    if yes:
        return True
    try:
        return ui.confirm(f"Remove {len(matches)} pod(s) selected by index?", hint="pass --yes to remove without a prompt")
    except EOFError:
        ui.warning("\nNo answer — nothing removed")
        return False


def human_approved_removing_every_pod(pods: List[PodInfo]) -> bool:
    """Ask before wiping the whole account — and fail closed when nobody can answer.

    A piped caller (or one that set ``LIUM_NONINTERACTIVE``) cannot answer a
    prompt; wiping every pod on the strength of a missing prompt is the one thing
    this module exists to prevent, so the command fails with
    ``confirmation_required`` and names ``--yes`` (the caller of this function
    already skips it when ``--yes`` was given).
    """
    listed_huids = ", ".join(pod.huid for pod in pods)
    try:
        return ui.confirm(f"Remove all {len(pods)} pods ({listed_huids})?", hint="pass --yes to remove every pod without a prompt")
    except EOFError:
        # The terminal went away mid-prompt. No answer is not a yes. (A refused prompt —
        # nobody to answer — is the CliFailure ui.confirm raises; it propagates.)
        ui.warning("\nNo answer — nothing removed")
        return False


@click.command("rm")
@click.argument("targets", required=False)
@click.option("--all", "-a", "remove_all", is_flag=True, help="Remove all active pods")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@click.option("--in", "in_duration", help="Schedule removal after duration")
@click.option("--at", "at_time", help="Schedule removal at time")
@click.option(
    "--name-only",
    "name_only",
    is_flag=True,
    help="Treat TARGETS as ids, names or huids only; never as 'lium ps' row numbers (for scripts).",
)
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits the removed pods with uptime and estimated spend.",
)
@handle_errors
def rm_command(
    targets: Optional[str],
    remove_all: bool,
    yes: bool,
    in_duration: Optional[str],
    at_time: Optional[str],
    name_only: bool,
    output_format: str,
):
    """Remove (terminate) GPU pods.

    \b
    TARGETS: comma-separated pod huids, names or ids (eager-wolf-aa,my-pod).
    A row number from your last 'lium ps' in this shell (1, 2) stands for the
    pod that listing showed there; it is accepted only while that pod is still
    listed and for 10 minutes after the listing. The pod list is account-wide
    and changes as pods come and go. Use --name-only or LIUM_NO_POD_INDEX=1 to
    refuse numbers altogether.

    \b
    Removal is irreversible. Exits non-zero when nothing matched TARGETS, so a
    typo cannot look like a successful teardown.
    \b
    Each removed pod is reported with its uptime and estimated spend
    (uptime × $/h, marked ≈ because the API returns no billed figure).
    """
    lium = Lium()
    # --format json: stdout is one JSON document, so the workspace context line goes to stderr
    show_workspace(lium, acting=True, on_stderr=output_format == "json")
    plan = build_removal_plan(
        lium, targets, remove_all, in_duration, at_time,
        allow_index=False if name_only else None, quiet=output_format == "json",
    )
    if plan is None:
        if output_format == "json":
            click.echo(json.dumps({"removed": [], "failed": []}))
        return

    if remove_all and not yes and not human_approved_removing_every_pod(plan.pods):
        return

    if plan.index_matches and not human_approved_index_targets(
        plan.index_matches, yes, on_stderr=output_format == "json"
    ):
        return

    context = {"pods": plan.pods, "lium": lium}
    if plan.termination_time:
        context["termination_time"] = plan.termination_time.isoformat()
        action = ScheduleRemovalAction()
        done_verb = "Scheduled removal for"
    else:
        action = RemovePodsAction()
        done_verb = "Removed"

    failed_huids = action.execute(context).data["failed_huids"]
    done_pods = [pod for pod in plan.pods if pod.huid not in failed_huids]
    removed_huids = [pod.huid for pod in done_pods]

    # The pods were listed before the delete, so their $/h and start time are
    # still in hand: report the final spend now, or the caller has to rebuild
    # it from `ps` history.
    now = datetime.now(timezone.utc)
    spends = {pod.huid: display.pod_spend(pod, now) for pod in done_pods}

    if output_format == "json":
        payload = {
            "scheduled" if plan.termination_time else "removed": [
                {"id": pod.id, "huid": pod.huid, "name": pod.name, **spends[pod.huid]} for pod in done_pods
            ],
            "failed": failed_huids,
        }
        if plan.termination_time:
            payload["termination_time"] = context["termination_time"]
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        if failed_huids:
            # The payload already names the failures; a second message on stdout
            # would break json.loads for the caller. The exit code says it failed.
            raise SystemExit(EXIT_GENERAL_ERROR)
    elif removed_huids:
        # Say what happened: silence is indistinguishable from having done nothing.
        ui.success(f"{done_verb} {len(removed_huids)} pod(s): {', '.join(removed_huids)}")
        if not plan.termination_time:
            for pod in done_pods:
                ui.info(display.format_removed_line(pod, spends[pod.huid]))

    if failed_huids:
        raise CliFailure(
            "removal_failed",
            f"Failed to remove pods: {', '.join(failed_huids)}",
            EXIT_GENERAL_ERROR,
        )
