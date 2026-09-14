"""CLI utilities and decorators."""
from functools import wraps
from contextlib import contextmanager
from typing import List, Dict, Any, Tuple, Optional, Callable, TypeVar
import json
import os
import sys
import traceback
from pathlib import Path
import click
from lium.cli.interactive import is_interactive, noninteractive_reason
from lium.cli.settings import config
from datetime import datetime, timezone
from rich.status import Status
from lium.sdk import (
    ExecutorInfo,
    Lium,
    LiumAuthError,
    LiumError,
    LiumHostKeyError,
    LiumInsufficientBalanceError,
    LiumNotFoundError,
    LiumPermissionError,
    LiumRateLimitError,
    LiumServerError,
    LiumSessionError,
    PodInfo,
)
from . import telemetry
from .themed_console import ThemedConsole
from dataclasses import dataclass
from rich.markup import escape
from rich.prompt import Prompt

T = TypeVar("T")

console = ThemedConsole()
_notice_console = None


def notice_console() -> ThemedConsole:
    """Return the stderr console for startup notices, building it on first use.

    Startup notices print before argument parsing, so they must not pollute the
    stdout of a command invoked with ``--json``. Built lazily because resolving
    the theme costs a subprocess on macOS and most commands never print one.
    """
    global _notice_console
    if _notice_console is None:
        _notice_console = ThemedConsole(stderr=True)
    return _notice_console


# Text formatting helpers

def mid_ellipsize(s: str, width: int = 28) -> str:
    """Truncate string with middle ellipsis if too long."""
    if not s:
        return "—"
    if len(s) <= width:
        return s
    keep = width - 1
    left = keep // 2
    right = keep - left
    return f"{s[:left]}…{s[-right:]}"


def pod_gpu_count(pod: PodInfo) -> Optional[int]:
    """GPUs to show for a pod: ``PodInfo.gpu_count`` when the API sent one, else the host's.

    ``PodInfo.gpu_count`` is the pod row's own billed count from ``/pods`` (DAH-2877);
    ``pod.executor`` describes the whole host. ``ps``, ``describe`` and their JSON
    views read this so a GPU-split rental (1 GPU of a 3×RTX 3090 node) reads
    ``RTX3090``, not ``3×RTX3090``. ``getattr`` because the CLI tests' pod doubles
    predate the field.
    """
    billed = getattr(pod, "gpu_count", None)
    if billed:
        return billed
    return pod.executor.gpu_count if pod.executor else None


def parse_timestamp(timestamp: str) -> Optional[datetime]:
    """Parse ISO format timestamp."""
    from datetime import datetime, timezone
    try:
        if timestamp.endswith('Z'):
            return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        elif '+' not in timestamp and '-' not in timestamp[10:]:
            return datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
        else:
            return datetime.fromisoformat(timestamp)
    except (ValueError, AttributeError):
        return None


def format_date(timestamp: str) -> str:
    """Format timestamp as relative or absolute date."""
    from datetime import datetime, timezone
    if not timestamp:
        return "—"

    dt = parse_timestamp(timestamp)
    if not dt:
        return "—"

    now = datetime.now(timezone.utc)
    delta = now - dt

    # If less than 24 hours, show relative time
    if delta.total_seconds() < 86400:  # 24 hours
        hours = delta.total_seconds() / 3600
        if hours < 1:
            mins = delta.total_seconds() / 60
            return f"{mins:.0f}m ago"
        else:
            return f"{hours:.1f}h ago"
    # If less than 7 days, show days
    elif delta.days < 7:
        return f"{delta.days}d ago"
    # Otherwise show date
    else:
        return dt.strftime("%Y-%m-%d")


def _prompt_value(
    prompt_text: str,
    default_value: T,
    value: T,
    cast: Callable[[str], T],
    validate: Callable[[T], bool],
) -> T:
    """Loop: Enter -> default, invalid -> reprompt until valid.

    Without a terminal there is no loop: the default is the answer.
    """
    default_str = str(default_value)
    if value != default_value or not is_interactive():
        return value
    while True:
        raw = Prompt.ask(prompt_text, default=default_str)
        # Empty/Enter -> use default
        if raw.strip() == "":
            return default_value
        try:
            value = cast(raw)
        except Exception:
            console.error("Invalid value, please try again")
            continue
        if not validate(value):
            console.error("Invalid value, please try again")
            continue
        return value


@dataclass
class BackupParams:
    """Backup configuration parameters."""
    enabled: bool = False
    path: str = config.default_backup_path
    frequency: int = config.default_backup_frequency  # hours
    retention: int = config.default_backup_retention  # days
    
    def validate(self) -> None:
        """Validate backup parameters."""
        if not self.enabled:
            return
            
        if not self.path.startswith('/'):
            raise ValueError(f"Backup path must be absolute (start with /), got: {self.path}")
        
        if self.frequency <= 0:
            raise ValueError(f"Backup frequency must be positive, got: {self.frequency}")
        
        if self.retention <= 0:
            raise ValueError(f"Backup retention must be positive, got: {self.retention}")
    
    def display_info(self) -> str:
        """Return formatted display info for backup configuration."""
        if not self.enabled:
            return "Backup: disabled"
        
        freq_display = f"{self.frequency}h" if self.frequency != 24 else "daily"
        ret_display = f"{self.retention} days" if self.retention != 7 else "1 week"
        
        return f"Backup: {self.path} every {freq_display}, retained for {ret_display}"


@contextmanager
def loading_status(message: str, success_message: str = ""):
    """Universal context manager to show loading status.

    A failure is not reported here. ``handle_errors`` is the one place that
    renders an error, and a spinner that prints its own line first makes a
    single failure read as two separate problems.
    """
    status = Status(f"{console.get_styled(message + '...', 'info')}", console=console)
    status.start()
    try:
        yield
        if success_message:
            console.success(f"✓ {success_message}")
    finally:
        status.stop()


def _update_spinner_display(step_prefix: str, message: str, start_time: float, running_flag):
    """Internal function to update spinner display with time."""
    import time
    import sys
    
    # Spinner characters (dots spinner)
    spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    spinner_index = 0
    
    # Get green color code from console theme
    green_color = console.theme.get('success', 'green')
    # Convert Rich color to ANSI escape sequence
    if green_color == 'green':
        green_code = '\033[32m'  # ANSI green
    else:
        green_code = '\033[32m'  # fallback to green
    reset_code = '\033[0m'  # ANSI reset
    
    while running_flag[0]:  # Use list for mutable reference
        elapsed = time.time() - start_time
        spinner_char = spinner_chars[spinner_index % len(spinner_chars)]
        
        # Use carriage return to overwrite the line smoothly with green spinner
        line = f"{step_prefix}{message}... {green_code}{spinner_char}{reset_code} ({elapsed:.1f}s)"
        sys.stdout.write(f"\r{line}")
        sys.stdout.flush()
        spinner_index += 1
        time.sleep(0.1)


def _handle_step_completion(step_prefix: str, message: str, elapsed: float, exception: Optional[Exception] = None):
    """Internal function to handle step completion display."""
    import sys
    
    # Clear the line and print final result
    sys.stdout.write('\r\033[K')  # Clear entire line
    
    if exception is None:
        # Success case
        done_styled = console.get_styled("done", 'success')
        console.print(f"{step_prefix}{message}... {done_styled} ({elapsed:.1f}s)", highlight=False)
    else:
        # General failure case
        failed_styled = console.get_styled("failed", 'error')
        console.print(f"{step_prefix}{message}... {failed_styled} ({elapsed:.1f}s)", highlight=False)


@contextmanager
def timed_step_status(step: int = 0, total_steps: int = 0, message: str = ""):
    """Context manager to show timed step status like [1/3] Renting machine... ⠋ (3.2s) -> [1/3] Renting machine... done (3.2s)."""
    import time
    import threading
    import sys
    
    start_time = time.time()
    # Only show step prefix if steps > 0 (with bullet for visual separation)
    step_prefix = f"● [{step}/{total_steps}] " if step > 0 and total_steps > 0 else ""
    running = [True]  # Use list for mutable reference
    
    # Hide cursor during animation
    sys.stdout.write('\033[?25l')  # Hide cursor
    sys.stdout.flush()
    
    # Start the update thread
    update_thread = threading.Thread(target=_update_spinner_display, args=(step_prefix, message, start_time, running), daemon=True)
    update_thread.start()
    
    try:
        yield
        # Stop the update and show success
        running[0] = False
        update_thread.join(timeout=0.1)
        
        # Show cursor again
        sys.stdout.write('\033[?25h')  # Show cursor
        
        elapsed = time.time() - start_time
        _handle_step_completion(step_prefix, message, elapsed)
        
    except Exception as e:
        # Stop the update and show appropriate message
        running[0] = False
        update_thread.join(timeout=0.1)
        
        # Show cursor again
        sys.stdout.write('\033[?25h')  # Show cursor
        
        elapsed = time.time() - start_time
        _handle_step_completion(step_prefix, message, elapsed, e)
        raise


# The exit-code taxonomy every command shares. Commands import these rather than
# spelling the numbers, so this table is the one source of truth and
# docs/exit-codes.md mirrors it (test_cli_errors.py checks that it does).
#
# ``lium provider …`` is the one exception: it keeps its own map in
# lium/cli/provider/_render.py, where the same numbers carry different meanings
# (2 auth, 3 portal, 5 ssh, 6 config missing, 7 token-cache contention).
EXIT_GENERAL_ERROR = 1        # a failure with no better classification
EXIT_CONFIGURATION_ERROR = 2  # bad arguments, missing or unreadable configuration
EXIT_API_ERROR = 3            # the API refused or failed the call
EXIT_SSH_ERROR = 4            # ssh could not connect, or no client is installed
EXIT_POD_NOT_FOUND = 5        # the named pod does not exist
EXIT_PERMISSION_DENIED = 6    # the account is not allowed to do this

OUTPUT_ENV = "LIUM_OUTPUT"    # LIUM_OUTPUT=json: every failure is a JSON envelope

# What to do next, by error code. A failure without a next step leaves an agent
# (or a person) guessing; a code that has no entry here falls back to the
# exit-code family below, so no error leaves without one.
_HINTS_BY_CODE: Dict[str, str] = {
    "no_api_key": "Set LIUM_API_KEY, or run 'lium init' (headless: 'lium init --no-browser'); "
                  "no account yet? 'lium signup --email you@example.com' creates one and stores its key",
    "invalid_api_key": "Check the key: 'lium config get api.api_key' shows which one is used; "
                       "a new one comes from https://lium.io/api-keys",
    "session_required": "Run 'lium workspaces login' (or set LIUM_SESSION_TOKEN); "
                        "an API key does not open the session-only commands",
    "permission_denied": "Check the account with 'lium balance'; an insufficient balance is "
                         "fixed with 'lium topup' or 'lium fund', a pending verification on https://lium.io",
    "insufficient_balance": "Add funds with 'lium topup' or 'lium fund', or pick a cheaper node "
                            "('lium ls --sort price_total')",
    "pod_not_found": "Run 'lium ps' to list pods; a name, huid, id or 1-based index is accepted",
    "not_found": "The resource is gone or the id is wrong; list it again and retry",
    "rate_limited": "Wait a few seconds and retry; back off if it repeats",
    "server_error": "Retry; if it persists, re-run with LIUM_DEBUG=1 and report the request",
    # Raised by the non-interactive guard (lium/cli/ui.py confirm/prompt, DAH-2883).
    "confirmation_required": "Re-run with --yes",
    "input_required": "Pass the value as an option instead of answering a prompt",
    "invalid_arguments": "See 'lium <command> --help' for the accepted options",
    "ssh_unavailable": "Wait for 'lium ps' to show the pod RUNNING with an SSH command, then retry",
    "ssh_connection_failed": "Check 'lium config get ssh.key_path' points at the key registered with "
                             "'lium ssh-keys', and that the pod is RUNNING in 'lium ps'",
    # not a retry: the message names the pinned file to delete once the new key is trusted
    "ssh_host_key_changed": "Do not retry blindly; if the pod was rebooted or re-templated and you trust the "
                            "new key, delete the known_hosts file named in the message and reconnect",
    "unexpected_error": "Re-run with LIUM_DEBUG=1 for details and report the issue",
}

_HINTS_BY_EXIT_CODE: Dict[int, str] = {
    EXIT_GENERAL_ERROR: "Re-run with LIUM_DEBUG=1 for details",
    EXIT_CONFIGURATION_ERROR: "Check the options and configuration ('lium <command> --help', 'lium config show')",
    EXIT_API_ERROR: "Retry; if it persists, re-run with LIUM_DEBUG=1 and report the request",
    EXIT_SSH_ERROR: "Check the pod is RUNNING in 'lium ps' and that 'lium config get ssh.key_path' "
                    "names a key registered with 'lium ssh-keys'",
    EXIT_POD_NOT_FOUND: "Run 'lium ps' to list pods; a name, huid, id or 1-based index is accepted",
    EXIT_PERMISSION_DENIED: "Check the account with 'lium balance'",
}


def default_hint(code: str, exit_code: int = EXIT_GENERAL_ERROR) -> str:
    """The next step for an error that did not bring its own."""
    return _HINTS_BY_CODE.get(code) or _HINTS_BY_EXIT_CODE.get(exit_code) or _HINTS_BY_EXIT_CODE[EXIT_GENERAL_ERROR]


class CliFailure(Exception):
    """A command failing for a reason it can name.

    Raised instead of exiting inline so that rendering — JSON envelope for a
    machine caller, Rich text for a human — and the exit code are decided in one
    place, ``handle_errors``, rather than at every error site in every command.

    ``hint`` is what to do next. Sites that know a better next step than the
    generic one for their code pass it; the rest get :func:`default_hint`.
    """

    def __init__(self, code: str, message: str, exit_code: int = EXIT_GENERAL_ERROR,
                 data: dict | None = None, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.data = data or {}
        self.hint = hint or default_hint(code, exit_code)


def error_envelope(code: str, message: str, exit_code: int = EXIT_GENERAL_ERROR,
                   data: dict | None = None, hint: str | None = None) -> dict:
    """The one shape every machine-readable failure has.

    ``{"ok": false, "error": {"code", "message", "hint", "exit_code"}}`` plus
    ``data`` when the caller has something the reader must not lose.
    """
    envelope = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "hint": hint or default_hint(code, exit_code),
            "exit_code": exit_code,
        },
    }
    if data:
        envelope["data"] = data
    return envelope


def _emit_json_error(code: str, message: str, exit_code: int = EXIT_GENERAL_ERROR,
                     data: dict | None = None, hint: str | None = None) -> None:
    """Print a machine-readable error envelope to stderr and exit non-zero.

    Keeps stdout clean for JSON consumers (agents): on success stdout carries
    the result JSON, on failure stdout is empty, the error JSON goes to stderr,
    and the process exits with a non-zero status so callers can detect it.
    ``data`` carries whatever the caller must not lose along with the failure
    (signup, for one, has to hand back the credentials it generated).
    """
    click.echo(json.dumps(error_envelope(code, message, exit_code, data, hint), sort_keys=True), err=True)
    raise SystemExit(exit_code)


def _render_human_error(message: str, hint: str, request_id: str | None = None) -> None:
    """The text rendering: the error, then the next step underneath it, then the id to
    quote to support when the API sent one (DAH-3057)."""
    console.error(escape(message))
    if hint and hint.lower() not in message.lower():
        console.dim(escape(hint))
    if request_id:
        console.dim(escape(f"request_id: {request_id}"))


def resolve_output_format(output_format: Optional[str], json_output: bool) -> str:
    """The format a command should render: ``--json`` is an alias for ``--format json``.

    Commands grew two spellings for the same thing (``describe --json`` versus
    ``ps --format json``), and a caller who learned one kept trying it on the
    other. Both are accepted; the ``--format`` value wins only when no alias
    was given.
    """
    if json_output:
        return "json"
    return output_format or "table"


def debug_enabled() -> bool:
    """``LIUM_DEBUG=1`` (or true): failures also print their traceback to stderr."""
    return os.environ.get("LIUM_DEBUG", "").strip().lower() in ("1", "true")


def json_output_requested() -> bool:
    """``LIUM_OUTPUT=json`` asks for machine-readable failures without a per-command flag."""
    return os.environ.get(OUTPUT_ENV, "").strip().lower() == "json"


def _wants_json(kwargs: dict) -> bool:
    """Whether the command was invoked for a machine reader, under any spelling."""
    return (
        bool(kwargs.get("json_output"))
        or kwargs.get("output_format") == "json"
        or json_output_requested()
    )


def _classify_sdk_error(error: LiumError) -> tuple[str, int]:
    """``(code, exit_code)`` for an SDK exception, most specific class first."""
    if isinstance(error, LiumHostKeyError):
        # the pod's pinned ssh host key changed (client.py ssh_connection): an ssh failure the
        # user must look at, not an API call to retry — exit 4 like the other ssh errors
        return "ssh_host_key_changed", EXIT_SSH_ERROR
    if isinstance(error, LiumInsufficientBalanceError):
        return "insufficient_balance", EXIT_PERMISSION_DENIED
    if isinstance(error, LiumPermissionError):
        return "permission_denied", EXIT_PERMISSION_DENIED
    if isinstance(error, LiumSessionError):
        # a missing/refused browser session (workspaces, keys): same exit 3 as any other
        # refused call, but the hint must name the login, not an API key
        return "session_required", EXIT_API_ERROR
    if isinstance(error, LiumAuthError):
        # Exit 3, as before: a 401 is the API refusing the call, and callers
        # (the live e2e suite among them) pin that number.
        return "invalid_api_key", EXIT_API_ERROR
    if isinstance(error, LiumNotFoundError):
        return "not_found", EXIT_API_ERROR
    if isinstance(error, LiumRateLimitError):
        return "rate_limited", EXIT_API_ERROR
    if isinstance(error, LiumServerError):
        return "server_error", EXIT_API_ERROR
    return "lium_error", EXIT_API_ERROR


def sdk_error_failure(error: LiumError, data: dict | None = None) -> CliFailure:
    """The failure ``handle_errors`` raises for an SDK error, with ``data`` attached.

    For a command that caught the error to finish its report first (``whoami``) and must still fail
    with the same code, exit status and hint as every other command — plus the report as ``data``.
    Like ``handle_errors``, it prefers the API's own code, hint and request_id when the
    server sent them (DAH-3057).
    """
    code, exit_code = _classify_sdk_error(error)
    merged = {**(_api_error_data(error) or {}), **(data or {})} or None
    return CliFailure(error.code or code, str(error), exit_code, data=merged, hint=error.hint)


def _api_error_data(e: LiumError) -> dict | None:
    """The server's request_id, for the JSON envelope's ``data`` (the hint has its own
    field in the envelope; see :func:`error_envelope`)."""
    return {"request_id": e.request_id} if e.request_id else None


def handle_errors(func):
    """Decorator to handle CLI errors gracefully.

    Messages are escaped before rendering: they carry things like
    ``lium.io[provider]``, and Rich reads square brackets as style tags.

    Every handled error exits non-zero. A caller chaining commands with ``&&``
    can only see failure through the exit code, so reporting an error and then
    exiting 0 tells it the command worked.

    When the wrapped command was invoked with ``--json`` (a ``json_output``
    flag), ``--format json`` (an ``output_format`` option), or under
    ``LIUM_OUTPUT=json``, errors are rendered as a JSON envelope on stderr, so
    machine consumers get parseable output instead of Rich-formatted text on
    stdout. Otherwise the human-readable rendering is preserved, with the hint
    on its own line under the error.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        json_output = _wants_json(kwargs)

        def fail(code: str, message: str, exit_code: int, data: dict | None = None,
                 hint: str | None = None, request_id: str | None = None, prefix: str = "") -> None:
            # ``prefix`` ("Error: ") is for the human line only; the JSON
            # message stays the bare text a program can match on.
            if debug_enabled():
                # The hints say "re-run with LIUM_DEBUG=1 for details": this is
                # the detail. Always stderr, so JSON on stdout stays clean.
                traceback.print_exc(file=sys.stderr)
            if json_output:
                _emit_json_error(code, message, exit_code, data, hint)
            _render_human_error(prefix + message, hint or default_hint(code, exit_code), request_id)
            raise SystemExit(exit_code)

        try:
            return func(*args, **kwargs)
        except (click.ClickException, click.Abort):
            raise
        except CliFailure as e:
            # a command that wrapped an API refusal (lium up → rent_rejected) hands the server's
            # request_id over in ``data`` and its hint as the failure's own (DAH-3057)
            fail(e.code, e.message, e.exit_code, e.data, e.hint, request_id=(e.data or {}).get("request_id"))
        except ValueError as e:
            if "No API key found" in str(e):
                fail("no_api_key", str(e), EXIT_CONFIGURATION_ERROR)
            fail("value_error", str(e), EXIT_CONFIGURATION_ERROR, prefix="Error: ")
        except LiumError as e:
            code, exit_code = _classify_sdk_error(e)
            # the API's own code, hint and request_id when it sent them (DAH-3057); the
            # class-derived code and the default hint otherwise
            fail(e.code or code, str(e), exit_code, _api_error_data(e), e.hint,
                 request_id=e.request_id, prefix="Error: ")
        except Exception as e:
            # a bug, not a usage or API error: the only branch crash reporting sees (DAH-2057)
            reported = telemetry.report(e)
            hint = default_hint("unexpected_error", EXIT_GENERAL_ERROR)
            if not reported and not json_output:
                # the nudge is for a person; the JSON envelope keeps the generic next step
                hint = f"{hint}\n{telemetry.OPT_IN_HINT}"
            fail("unexpected_error", str(e), EXIT_GENERAL_ERROR, prefix="Unexpected error: ", hint=hint)
    return wrapper


def extract_executor_metrics(executor: ExecutorInfo) -> Dict[str, float]:
    """Extract relevant metrics from an executor for Pareto comparison."""
    specs = executor.specs or {}
    
    # GPU metrics
    gpu_info = specs.get("gpu", {})
    gpu_details = gpu_info.get("details", [{}])[0] if gpu_info.get("details") else {}
    
    # System metrics
    ram_data = specs.get("ram", {})
    disk_data = specs.get("hard_disk", {})

    # Location preference (US gets a bonus)
    location = executor.location or {}
    country = location.get("country", "").upper()
    country_code = location.get("country_code", "").upper()
    is_us = country == "UNITED STATES" or country_code == "US"

    net_up = executor.upload_speed
    net_down = executor.download_speed

    return {
        'price_per_gpu': executor.price_per_gpu or float('inf'),
        'vram_gb': ((gpu_details.get("capacity") or 0) / 1024) if gpu_details else 0,  # MiB to GB
        'ram_gb': ((ram_data.get("total") or 0) / (1024 * 1024)) if ram_data else 0,  # KB to GB
        'disk_gb': ((disk_data.get("total") or 0) / (1024 * 1024)) if disk_data else 0,  # KB to GB
        'pcie_speed': gpu_details.get("pcie_speed") or 0,
        'memory_bandwidth': gpu_details.get("memory_speed") or 0,
        'tflops': gpu_details.get("graphics_speed") or 0,
        'net_up': net_up,
        'net_down': net_down,
        'location_score': 1.0 if is_us else 0.0,  # US locations get higher score
        'total_bandwidth': net_up + net_down,  # Combined bandwidth
    }


_MINIMIZE_METRICS = frozenset({'price_per_gpu'})
_SECONDARY_METRICS = ('total_bandwidth', 'location_score', 'net_up')
# Metrics excluded from the equal-price residual sweep: already handled above, or
# minimize-metrics whose direction the residual loop does not account for.
_EQUAL_PRICE_SKIP = frozenset({'net_down'} | set(_SECONDARY_METRICS) | _MINIMIZE_METRICS)


def dominates(metrics_a: Dict[str, float], metrics_b: Dict[str, float]) -> bool:
    """Check if executor A dominates executor B in Pareto sense.

    Download speed is always evaluated first: if A is >10% faster than B, A
    dominates immediately. If B is >10% faster, A cannot dominate. Only when
    speeds are within 10% of each other does the rest of the comparison run.
    """
    # --- Download speed: unconditional first priority ---
    net_down_a = metrics_a.get('net_down') or 0
    net_down_b = metrics_b.get('net_down') or 0
    dl_threshold = 0.1 * max(net_down_a, net_down_b)

    if net_down_a > net_down_b + dl_threshold:
        return True   # A is significantly faster — A dominates
    if net_down_b > net_down_a + dl_threshold:
        return False  # B is significantly faster — A cannot dominate

    # --- Download speeds are similar; fall back to price-based logic ---
    price_a_value = metrics_a.get('price_per_gpu')
    price_b_value = metrics_b.get('price_per_gpu')
    price_a = price_a_value if price_a_value is not None else float('inf')
    price_b = price_b_value if price_b_value is not None else float('inf')

    if abs(price_a - price_b) < 0.01:  # Prices are effectively equal
        # Check secondary metrics in priority order; both A and B must be
        # compared across ALL of them before declaring domination.
        at_least_one_better = False
        for metric in _SECONDARY_METRICS:
            val_a = metrics_a.get(metric, 0) or 0
            val_b = metrics_b.get(metric, 0) or 0
            threshold = 0.1 * max(val_a, val_b) if metric != 'location_score' else 0

            if val_a > val_b + threshold:
                at_least_one_better = True
            elif val_b > val_a + threshold:
                return False

        if at_least_one_better:
            return True

        # All secondary metrics similar — compare remaining maximize-metrics
        for metric in metrics_a:
            if metric in _EQUAL_PRICE_SKIP:
                continue
            val_a = metrics_a[metric] or 0
            val_b = metrics_b.get(metric, 0) or 0
            if val_a > val_b:
                at_least_one_better = True
            elif val_a < val_b:
                return False
        return at_least_one_better

    # Standard Pareto domination when prices differ.
    # Use a 0.1% relative tolerance to absorb floating-point noise from unit
    # conversions (e.g. KB→GB for RAM), so a 7 MB difference on a 1.5 TB node
    # doesn't block a genuine domination.
    at_least_one_better = False
    for metric in metrics_a:
        if metric == 'net_down':
            continue  # already decided above
        val_a = metrics_a[metric] or 0
        val_b = metrics_b.get(metric, 0) or 0
        eps = 0.001 * max(abs(val_a), abs(val_b))

        if metric in _MINIMIZE_METRICS:
            if val_a < val_b - eps:
                at_least_one_better = True
            elif val_a > val_b + eps:
                return False
        else:
            if val_a > val_b + eps:
                at_least_one_better = True
            elif val_a < val_b - eps:
                return False

    return at_least_one_better


MIN_DOWNLOAD_MBPS = 100.0  # mirrors MIN_VERIFYX_EMA_DOWNLOAD_SPEED_MBPS in the validator


def calculate_pareto_frontier(executors: List[ExecutorInfo]) -> List[bool]:
    """Calculate which executors are on the Pareto frontier.

    Returns a list of booleans indicating if each executor is Pareto-optimal.
    Executors below MIN_DOWNLOAD_MBPS are disqualified before domination checks.
    """
    metrics_list = [extract_executor_metrics(e) for e in executors]

    is_pareto = []
    for i, metrics_i in enumerate(metrics_list):
        if (metrics_i.get('net_down') or 0) < MIN_DOWNLOAD_MBPS:
            is_pareto.append(False)
            continue

        dominated = False
        for j, metrics_j in enumerate(metrics_list):
            if i != j and dominates(metrics_j, metrics_i):
                dominated = True
                break
        is_pareto.append(not dominated)

    return is_pareto


def store_executor_selection(executors: List[ExecutorInfo]) -> None:
    """Store the last executor selection for index-based selection."""
    from lium.cli.settings import config
    
    selection_data = {
        'timestamp': datetime.now().isoformat(),
        'executors': []
    }
    
    for executor in executors:
        selection_data['executors'].append({
            'id': executor.id,
            'huid': executor.huid,
            'gpu_type': executor.gpu_type,
            'gpu_count': executor.gpu_count,
            'price_per_hour': executor.price_per_hour,
            'location': executor.location.get('country', 'Unknown') if executor.location else 'Unknown'
        })
    
    # Store in config directory
    config_file = config.config_dir / "last_selection.json"
    with open(config_file, 'w') as f:
        json.dump(selection_data, f, indent=2)


def get_last_executor_selection() -> Optional[Dict[str, Any]]:
    """Retrieve the last executor selection."""
    from lium.cli.settings import config
    
    config_file = config.config_dir / "last_selection.json"
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def store_volume_selection(volumes: List) -> None:
    """Store the last volume selection for HUID-based lookup."""
    from lium.cli.settings import config

    selection_data = {
        'timestamp': datetime.now().isoformat(),
        'volumes': []
    }

    for volume in volumes:
        # Handle VolumeInfo objects
        selection_data['volumes'].append({
            'id': volume.id,
            'huid': volume.huid,
            'name': volume.name,
            'description': volume.description,
            'current_size_gb': volume.current_size_gb,
        })

    # Store in config directory
    config_file = config.config_dir / "last_volumes.json"
    with open(config_file, 'w') as f:
        json.dump(selection_data, f, indent=2)


def get_last_volume_selection() -> Optional[Dict[str, Any]]:
    """Retrieve the last volume selection."""
    from lium.cli.settings import config

    config_file = config.config_dir / "last_volumes.json"
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def resolve_volume_huid(huid: str) -> Optional[str]:
    """
    Resolve volume HUID to database ID from cached selection.
    Returns database ID or None if not found.
    """
    last_selection = get_last_volume_selection()
    if not last_selection:
        return None

    volumes = last_selection.get('volumes', [])
    for volume in volumes:
        if volume.get('huid') == huid:
            return volume.get('id')

    return None


def parse_volume_spec(volume_spec: str) -> Tuple[Optional[str], Optional[Dict[str, str]], Optional[str]]:
    """
    Parse volume specification.

    Formats:
      - id:<HUID>                        -> resolve HUID to database ID
      - new[:name=X[,desc=Y]]            -> create new volume

    Returns:
        (volume_id, create_params, error_message)
        - volume_id: existing volume database ID
        - create_params: dict with 'name' and 'description' for new volume
        - error_message: error description if parsing failed
    """
    spec = volume_spec.strip()

    # Format: id:<HUID>
    if spec.startswith('id:'):
        huid = spec[3:].strip()
        if not huid:
            return None, None, "Volume HUID is missing after 'id:'"

        volume_id = resolve_volume_huid(huid)
        if not volume_id:
            return None, None, f"Volume with HUID '{huid}' not found. Run 'lium volumes' first."

        return volume_id, None, None

    # Format: new[:name=X,desc=Y] or just new
    if spec.startswith('new'):
        create_params = {'name': '', 'description': ''}

        # Check if there are parameters
        if len(spec) > 3:
            if not spec[3] == ':':
                return None, None, f"Invalid format: expected 'new' or 'new:name=...' but got '{spec}'"

            params_str = spec[4:].strip()
            if params_str:
                # Parse key=value pairs
                for param in params_str.split(','):
                    param = param.strip()
                    if '=' not in param:
                        return None, None, f"Invalid parameter format: '{param}'. Expected 'key=value'"

                    key, value = param.split('=', 1)
                    key = key.strip()
                    value = value.strip()

                    if key == 'name':
                        create_params['name'] = value
                    elif key == 'desc':
                        create_params['description'] = value
                    else:
                        return None, None, f"Unknown parameter: '{key}'. Use 'name' or 'desc'"

        # Name is required for new volumes
        if not create_params['name']:
            return None, None, "Volume name is required. Use 'new:name=<NAME>' or 'new:name=<NAME>,desc=<DESC>'"

        return None, create_params, None

    # Unknown format
    return None, None, f"Invalid volume format: '{spec}'. Use 'id:<HUID>' or 'new:name=<NAME>[,desc=<DESC>]'"


def resolve_executor_indices(indices: List[str]) -> Tuple[List[str], Optional[str]]:
    """
    Resolve executor indices from the last selection.
    Returns (resolved_executor_ids, error_message)
    """
    last_selection = get_last_executor_selection()
    if not last_selection:
        return [], None
    
    executors = last_selection.get('executors', [])
    if not executors:
        return [], "No nodes in last selection."
    
    resolved_ids = []
    failed_resolutions = []
    
    for index_str in indices:
        try:
            index = int(index_str)
            if 1 <= index <= len(executors):
                executor_data = executors[index - 1]
                resolved_ids.append(executor_data['id'])
            else:
                failed_resolutions.append(f"Index {index_str} is out of range (1..{len(executors)}). Try: lium ls")
        except ValueError:
            failed_resolutions.append(f"{index_str} (not a valid index)")
    
    error_msg = None
    if failed_resolutions:
        error_msg = f"Could not resolve indices: {', '.join(failed_resolutions)}"
    
    return resolved_ids, error_msg


# Pod indexes ("lium rm 1") are the row numbers of the last `lium ps` *in this
# shell*. The pod list is account-wide and changes as pods come and go — on a
# shared account another caller's pod can move into row 1 between the `ps` and
# the `rm` — and `GET /pods` has no fixed order, so a row number is never taken
# at face value: it is translated to the pod id `ps` showed on that row, and
# that pod must still be listed. The snapshot is keyed by the parent process
# (the shell or agent that ran `ps`), so another agent's `lium ps` in the same
# home directory does not redefine this shell's numbers.
POD_INDEX_TTL_SECONDS = 600
POD_INDEX_ENV = "LIUM_NO_POD_INDEX"
_PS_SNAPSHOT_PREFIX = "last_ps."
_PS_SNAPSHOT_SUFFIX = ".json"


def pod_indexes_allowed() -> bool:
    """``LIUM_NO_POD_INDEX=1`` makes every command treat numeric targets as names only."""
    return os.environ.get(POD_INDEX_ENV, "").strip().lower() not in ("1", "true", "yes", "on")


def pod_index_session() -> str:
    """The key the `lium ps` snapshot is filed under: the parent process (shell, agent) of this run."""
    return str(os.getppid())


def pod_snapshot_path(session: Optional[str] = None) -> Path:
    from lium.cli.settings import config

    return config.config_dir / f"{_PS_SNAPSHOT_PREFIX}{session or pod_index_session()}{_PS_SNAPSHOT_SUFFIX}"


def _prune_pod_snapshots(keep: Path, now: datetime) -> None:
    """Drop other shells' snapshots once they are past the TTL; they can never be used again."""
    try:
        for path in keep.parent.glob(f"{_PS_SNAPSHOT_PREFIX}*{_PS_SNAPSHOT_SUFFIX}"):
            if path == keep:
                continue
            if now.timestamp() - path.stat().st_mtime > POD_INDEX_TTL_SECONDS:
                path.unlink()
    except OSError:
        # Pruning other shells' stale snapshots is best-effort housekeeping: a stat/unlink race with
        # another lium process, or an unreadable file, must never fail the command that ran `lium ps`.
        pass


def store_pod_selection(pods: List[PodInfo], now: Optional[datetime] = None) -> None:
    """Remember which pod `lium ps` showed on which row, so indexes can be checked later."""
    now = now or datetime.now(timezone.utc)
    snapshot = {
        "timestamp": now.isoformat(),
        "pods": [{"id": pod.id, "huid": pod.huid, "name": pod.name} for pod in pods],
    }
    path = pod_snapshot_path()
    try:
        with open(path, "w") as f:
            json.dump(snapshot, f, indent=2)
    except OSError:
        # Not being able to remember the list only means indexes will be refused.
        return
    _prune_pod_snapshots(path, now)


def get_pod_selection() -> Optional[Dict[str, Any]]:
    """This shell's last `lium ps` snapshot, or None when there is none or it is unreadable."""
    snapshot_file = pod_snapshot_path()
    if not snapshot_file.exists():
        return None
    try:
        with open(snapshot_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pods"), list):
        return None
    return data


def _snapshot_age_seconds(snapshot: Dict[str, Any], now: datetime) -> Optional[float]:
    try:
        stamp = datetime.fromisoformat(snapshot["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds()


@dataclass(frozen=True)
class TargetMatch:
    """One resolved target: the pod, the text that named it, and how it matched."""

    pod: PodInfo
    target: str
    via_index: bool = False


def _resolve_pod_index(
    target: str, all_pods: List[PodInfo], snapshot: Optional[Dict[str, Any]], now: datetime
) -> Optional[PodInfo]:
    """The pod a `lium ps` row number stands for, or None when TARGET is not an index.

    The number is translated to the pod id the last `lium ps` in this shell showed
    on that row, and that pod is looked up by id in the live list — its position
    today does not matter. Raises CliFailure when the number *looks* like an index
    but cannot be trusted: no `lium ps` in this shell, a `ps` too long ago, or a
    pod that is no longer listed. Refusing is the safe answer — the alternative is
    acting on a pod the caller never saw.
    """
    try:
        idx = int(target) - 1
    except ValueError:
        return None
    if idx < 0:
        return None

    example = f" (e.g. {all_pods[0].huid})" if all_pods else ""
    hint = f"Run 'lium ps' and retry, or name the pod by its huid{example}"

    if snapshot is None:
        raise CliFailure(
            "stale_pod_index",
            f"Pod index {target} cannot be used before 'lium ps' has shown the list in this shell. {hint}",
            EXIT_CONFIGURATION_ERROR,
        )
    age = _snapshot_age_seconds(snapshot, now)
    if age is None or age > POD_INDEX_TTL_SECONDS or age < 0:
        raise CliFailure(
            "stale_pod_index",
            f"Pod index {target} refers to a 'lium ps' listing older than {POD_INDEX_TTL_SECONDS // 60} minutes. {hint}",
            EXIT_CONFIGURATION_ERROR,
        )

    shown = snapshot["pods"]
    if idx >= len(shown):
        # The last ps had no such row; fall through to id/name/huid matching.
        return None
    seen_id, seen_huid = shown[idx].get("id"), shown[idx].get("huid")
    current = next((pod for pod in all_pods if pod.id == seen_id), None)
    if current is None:
        raise CliFailure(
            "stale_pod_index",
            f"Pod index {target} was {seen_huid} in the last 'lium ps' but that pod is no longer "
            f"listed — the pod list changed. {hint}",
            EXIT_CONFIGURATION_ERROR,
        )
    return current


def _exact_match(target: str, all_pods: List[PodInfo]) -> Optional[PodInfo]:
    return next((pod for pod in all_pods if target in (pod.id, pod.name, pod.huid)), None)


def resolve_targets(
    targets: str,
    all_pods: List[PodInfo],
    *,
    allow_index: Optional[bool] = None,
    now: Optional[datetime] = None,
) -> List[TargetMatch]:
    """Resolve a comma-separated TARGETS spec against the live pod list.

    Each target is a pod id, name, huid, or — when indexes are allowed and the
    last `lium ps` in this shell still applies — a row number of that listing.
    ``allow_index`` defaults to the ``LIUM_NO_POD_INDEX`` environment setting.
    A number that cannot be honoured as an index is still accepted when it is
    literally a pod's name, id or huid; otherwise it is refused.
    """
    if targets.lower() == "all":
        return [TargetMatch(pod, "all") for pod in all_pods]

    if allow_index is None:
        allow_index = pod_indexes_allowed()
    now = now or datetime.now(timezone.utc)
    snapshot = get_pod_selection() if allow_index else None

    matches: List[TargetMatch] = []
    for target in targets.split(","):
        target = target.strip()
        if not target:
            continue

        if allow_index:
            try:
                pod = _resolve_pod_index(target, all_pods, snapshot, now)
            except CliFailure:
                # "42" with no usable listing may still be the pod literally named 42.
                pod = _exact_match(target, all_pods)
                if pod is None:
                    raise
                matches.append(TargetMatch(pod, target))
                continue
            if pod is not None:
                matches.append(TargetMatch(pod, target, via_index=True))
                continue

        pod = _exact_match(target, all_pods)
        if pod is not None:
            matches.append(TargetMatch(pod, target))

    return matches


def parse_targets(targets: str, all_pods: List[PodInfo], *, allow_index: Optional[bool] = None) -> List[PodInfo]:
    """Parse target specification and return matching pods (see :func:`resolve_targets`)."""
    return [match.pod for match in resolve_targets(targets, all_pods, allow_index=allow_index)]


def wait_for_pod_ready(
    lium_client, pod_id: str, timeout: Optional[int] = None, on_poll: Optional[Callable[..., None]] = None
) -> Optional[PodInfo]:
    """Wait for a pod to be ready (RUNNING with SSH); without ``timeout`` there is no time limit.

    Delegates to :meth:`Lium.wait_ready`, so a pod that fails or disappears
    raises ``PodStartError`` instead of being polled forever. Returns ``None``
    only when ``timeout`` is given and the pod is still starting when it runs out.
    ``on_poll`` is forwarded so the caller can show progress between polls.
    """
    # poll_interval=None: 2 s for the first 90 s, then 10 s (DAH-3002, Lium.poll_delay).
    return lium_client.wait_ready(pod_id, timeout=timeout, poll_interval=None, on_poll=on_poll)


# The old name, kept only until the open PRs that still import it (lium#141, #155, #172) land;
# then it goes.
wait_ready_no_timeout = wait_for_pod_ready


def get_pytorch_template_id() -> Optional[str]:
    """Get the template ID for the newest PyTorch template."""
    
    lium = Lium()
    templates = lium.templates()

    if config.default_template_id in {t.id for t in templates}:
        return config.default_template_id
    
    # Filter PyTorch templates from daturaai/pytorch
    pytorch_templates = [
        t for t in templates 
        if t.category.upper() == "PYTORCH" 
        and t.docker_image == "daturaai/pytorch"
        and t.name.startswith("Pytorch (Cuda)")
    ]
    
    if not pytorch_templates:
        return None
    
    # Sort by PyTorch version (extract version from tag)
    def extract_pytorch_version(template):
        tag = template.docker_image_tag
        # Extract version like "2.6.0" from "2.6.0-py3.11-cuda12.5.1-devel-ubuntu24.04"
        version_part = tag.split('-')[0]
        try:
            # Split version into major.minor.patch for proper sorting
            parts = [int(x) for x in version_part.split('.')]
            return tuple(parts)
        except:
            return (0, 0, 0)

    # Get the template with highest version
    newest_template = max(pytorch_templates, key=extract_pytorch_version)
    return newest_template.id


def ensure_config():
    from .init.actions import SetupApiKeyAction, SetupSshKeyAction
    from lium.cli.settings import config

    if not config.get('api.api_key'):
        if not is_interactive():
            # The browser login needs a person at the keyboard. Without one it
            # would open a browser nobody sees and poll for half a minute
            # before failing — name the fix instead.
            raise CliFailure(
                "no_api_key",
                "No API key configured and the browser login cannot run because "
                f"{noninteractive_reason()}. Set LIUM_API_KEY, or run "
                "'lium init --no-browser' and then 'lium init --session <ID>'",
                EXIT_CONFIGURATION_ERROR,
                hint="Set LIUM_API_KEY, or run 'lium init --no-browser' and then 'lium init --session <ID>'; "
                     "no account yet? 'lium signup --email you@example.com' creates one and stores its key",
            )
        # Setup API key
        action = SetupApiKeyAction()
        result = action.execute({})
        if not result.ok:
            raise CliFailure("api_key_setup_failed", result.error, EXIT_CONFIGURATION_ERROR)

    if not config.get('ssh.key_path'):
        # Setup SSH key
        action = SetupSshKeyAction()
        result = action.execute({})
        if not result.ok:
            raise CliFailure("ssh_key_setup_failed", result.error, EXIT_CONFIGURATION_ERROR)


def ensure_backup_params(
    enabled: bool = True, 
    path: str = config.default_backup_path, 
    frequency: int = config.default_backup_frequency, 
    retention: int = config.default_backup_retention, 
    skip_prompts: bool = False
) -> BackupParams:
    """Create and validate backup parameters, prompt if needed.
    
    - If prompts run: Enter -> default, invalid -> ask again (uniform for all fields).
    - If prompts are skipped: uses passed values as-is.
    """
    if not enabled:
        return BackupParams(enabled=False)

    final_path, final_frequency, final_retention = path, frequency, retention

    # Keep original behavior: only prompt when using the built-in defaults and prompts not skipped
    default_tuple = (config.default_backup_path, config.default_backup_frequency, config.default_backup_retention)
    if not skip_prompts and (path, frequency, retention) == default_tuple:
        console.info("Configuring automated backups...")

        final_path = _prompt_value(
            "[cyan]Backup path[/cyan]",
            default_value=config.default_backup_path,
            value=path,
            cast=str,
            validate=lambda p: isinstance(p, str) and p.startswith("/"),
        )

        final_frequency = _prompt_value(
            "[cyan]Backup frequency in hours[/cyan] (e.g., 6, 12, 24)",
            default_value=config.default_backup_frequency,
            value=frequency,
            cast=int,
            validate=lambda x: isinstance(x, int) and x > 0,
        )

        final_retention = _prompt_value(
            "[cyan]Backup retention in days[/cyan] (e.g., 7, 14, 30)",
            default_value=config.default_backup_retention,
            value=retention,
            cast=int,
            validate=lambda x: isinstance(x, int) and x > 0,
        )

    params = BackupParams(
        enabled=True,
        path=final_path,
        frequency=final_frequency,
        retention=final_retention,
    )
    # Let validate() raise with a clear message if something is wrong
    params.validate()
    return params


def setup_backup(lium, pod: PodInfo, backup_params: BackupParams, replace_existing: bool = True) -> None:
    """Setup backup for a pod using lium SDK.
    
    Args:
        lium: Lium SDK instance
        pod: PodInfo for the target pod
        backup_params: Backup configuration parameters
    """
    if not backup_params.enabled:
        return
    
    pod_name = pod.name or pod.huid
    
    try:
        lium.backup_create(
            pod=pod,
            path=backup_params.path,
            frequency_hours=backup_params.frequency,
            retention_days=backup_params.retention
        )
    except Exception as e:
        if "Backup configuration already exists" in str(e) and replace_existing:
            # remove existing one
            backup_config = lium.backup_config(pod=pod)
            if backup_config:
                lium.backup_delete(backup_config.id)
            # try again
            return setup_backup(lium, pod, backup_params, replace_existing=False)
        elif "API error 400" in str(e):
            data_str = str(e).split("API error 400:")[-1].strip()
            data = json.loads(data_str) if data_str else {}
            if "message" in data:
                console.error(f"Failed to setup backup: {data['message']}")
                return

        console.error(f"Failed to setup backup: {e}")
        raise
