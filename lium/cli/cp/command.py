"""`lium cp`: copy files from one pod to another over SSH."""

import json
import warnings
from typing import Optional

import click

from lium.sdk import Lium, LiumError, LiumHostKeyError
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    EXIT_SSH_ERROR,
    handle_errors,
)
from . import parsing


@click.command("cp")
@click.argument("source", metavar="SRC_POD:PATH")
@click.argument("destination", metavar="DST_POD:PATH")
@click.option("--bwlimit", type=click.IntRange(min=1), metavar="KIB_PER_S", help="Cap the transfer rate (rsync --bwlimit)")
@click.option("--exclude", multiple=True, metavar="PATTERN", help="Skip matching paths; repeatable (rsync --exclude)")
@click.option("--delete", is_flag=True, help="Remove files at the destination that are not in the source")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def cp_command(
    source: str,
    destination: str,
    bwlimit: Optional[int],
    exclude: tuple,
    delete: bool,
    json_output: bool,
):
    """Copy files between two pods directly over SSH.

    The data goes pod to pod, not through this machine: a one-off key is
    created on the source pod, authorised on the destination for the duration
    of the copy, and removed afterwards. Both pods need rsync
    (apt-get install -y rsync); the destination also needs flock (util-linux,
    on every Ubuntu/Debian image) to serialise copies into the same pod.

    \b
    POD is a name, huid, id or index from 'lium ps'. A trailing '/' on a
    source directory copies its contents, as in rsync.
    \b
    Examples:
      lium cp dev-pod:/workspace/src train-pod:/workspace/
      lium cp 1:/workspace/ckpt/ 2:/workspace/ckpt/ --exclude '*.tmp' --bwlimit 50000
    """
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())
    if not all_pods:
        raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)

    parsed, error = parsing.parse(source, destination, all_pods)
    if error:
        code = "pod_not_found" if error.startswith("No pods match") else "invalid_arguments"
        raise CliFailure(
            code, error, EXIT_POD_NOT_FOUND if code == "pod_not_found" else EXIT_CONFIGURATION_ERROR
        )
    src, dst = parsed
    # Both ends must be reachable over ssh before any key is generated or a pod is touched; the same
    # code and exit `lium ssh` gives a pod that is not RUNNING yet (ssh_unavailable, 4), not value_error.
    for end in (src, dst):
        if end.pod.status != "RUNNING" or not end.pod.ssh_cmd:
            raise CliFailure(
                "ssh_unavailable",
                f"Pod '{end.pod.huid}' is {end.pod.status}" + ("" if end.pod.ssh_cmd else " with no SSH connection yet"),
                EXIT_SSH_ERROR,
                hint="Wait for 'lium ps' to show it RUNNING with an SSH command, then retry",
            )

    # The SDK reports a failed cleanup (a transfer key left authorised on the
    # destination) as a warning; show it as one, with the revoke command in it —
    # on stderr, so `--json` stdout stays one document (the first copy into a
    # fresh pod always warns about pinning its host key).
    # A copy that fails on a pod (rsync missing, the grant refused, rsync's own exit) is not an API
    # failure: it exits 1 as `copy_failed` with the next step, not 3 as `lium_error` with "retry".
    # The no-pin refusal keeps ssh's exit 4 but names its own next step (there is no file to delete).
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = ui.load(
                f"Copying {src.pod.huid}:{src.path} -> {dst.pod.huid}:{dst.path}",
                lambda: lium.cp(
                    src.pod, src.path, dst.pod, dst.path,
                    bwlimit=bwlimit, exclude=list(exclude), delete=delete,
                ),
            )
    except LiumHostKeyError as e:
        if "No pinned host key" not in str(e):
            raise
        raise CliFailure(
            "ssh_host_key_unknown", str(e), EXIT_SSH_ERROR,
            hint=f"Connect to the destination once with 'lium ssh {dst.pod.huid}' so its host key is pinned, "
                 f"or set LIUM_SSH_INSECURE=1 to skip host key checks",
        ) from e
    except LiumError as e:
        if getattr(e, "code", None) or type(e) is not LiumError:
            raise  # the API's own refusal (401/403/404…) keeps its class, code and exit
        raise CliFailure(
            "copy_failed", str(e), EXIT_GENERAL_ERROR,
            hint="Both pods need rsync (apt-get install -y rsync) and the destination needs flock (util-linux); "
                 "the message carries rsync's own error",
        ) from e
    for warning in caught:
        ui.notice_warning(str(warning.message))

    if json_output:
        click.echo(json.dumps({
            "ok": True,
            "source": {"pod": src.pod.huid, "path": src.path},
            "destination": {"pod": dst.pod.huid, "path": dst.path},
            "exit_code": result.get("exit_code", 0),
        }, sort_keys=True))
        return

    ui.success(f"Copied {src.pod.huid}:{src.path} -> {dst.pod.huid}:{dst.path}")
