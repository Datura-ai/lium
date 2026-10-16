"""The tools `lium mcp` serves: thin calls into `lium.sdk.Lium` that return plain JSON-safe dicts.

An exception never reaches the MCP host as a traceback: every tool returns ``{"ok": false, "error": {code, message,
hint}}`` instead, so the model can read what went wrong and what to do next.
"""

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from lium.sdk import Lium
from lium.sdk.exceptions import (
    LiumError,
    LiumNotFoundError,
    LiumRentOutcomeUnknownError,
    OutputLimitExceeded,
    PodStartError,
)
from lium.sdk.models import ExecutorInfo, PodInfo

# exec output is cut to its tail: a training log of megabytes would fill the model's context
OUTPUT_TAIL_CHARS = 20_000
# a command that floods output (yes, a runaway print) fails at this many bytes instead of filling the server's memory
MAX_OUTPUT_BYTES = 1024 * 1024


def _error(code: str, message: str, hint: Optional[str] = None) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}


def _guarded(call: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return call()
    except LiumError as e:
        return _error(e.code or type(e).__name__, str(e), e.hint)
    except Exception as e:  # noqa: BLE001 - the host must get an answer, not a dead stdio session
        return _error(type(e).__name__, str(e))


def _node(node: ExecutorInfo) -> Dict[str, Any]:
    location = node.location or {}
    return {
        "executor_id": node.id,
        "huid": node.huid,
        "gpu_type": node.gpu_type,
        "gpu_count": node.gpu_count,
        "available_gpu_count": node.available_gpu_count,
        "price_per_gpu_hour_usd": node.price_per_gpu,
        "price_per_hour_usd": node.price_per_hour,
        "country": location.get("country") or location.get("country_code"),
        "tier": node.tier,
        "max_cuda_version": node.max_cuda_version,
    }


def _pod(pod: PodInfo) -> Dict[str, Any]:
    return {
        "pod_id": pod.id,
        "name": pod.name,
        "huid": pod.huid,
        "status": pod.status,
        "ssh_cmd": pod.ssh_cmd,
        "gpu_type": pod.executor.gpu_type if pod.executor else None,
        "gpu_count": pod.gpu_count or (pod.executor.gpu_count if pod.executor else None),
        "price_per_hour_usd": pod.executor.price_per_hour if pod.executor else None,
        "created_at": pod.created_at,
    }


def _redact(text: Optional[str], command: str) -> Optional[str]:
    """`text` with the command cut out: commands often carry secrets (HF_TOKEN=... python train.py)."""
    return text.replace(command, "<command>") if text and command else text


class _RentalCancelled(Exception):
    pass


class _Rental:
    """One rent_pod call, shared by its worker thread and the async tool, which the host may cancel at any point.

    Once ``submitted`` (``up`` was called) a pod may exist. ``outcome`` is None while ``up`` is in flight, then
    "created" (the API named the pod), "rejected" (the SDK established that no request could have rented) or
    "unknown" (a request went unanswered: maybe a pod). The request (``replay``) is journaled to disk before it is
    sent, with its account scope and send time, so an unknown rental can be re-sent with the same idempotency key,
    also by a restarted server. ``pod_ids`` are the pods the API named for this rental that are not yet confirmed
    removed. ``candidates`` are new pods with the rental's name: shown, never deleted, since a name proves nothing
    about who rented the pod. ``recovered``: the pod of a rental the host still wants is known (a replay named it,
    or a restart found it in the journal) and waits to be handed to a tool call. ``handed``: recovered rentals this
    call returns; they stay journaled until the result reaches the host (:meth:`LiumTools.deliver`)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.name = ""
        self.cancelled = False
        self.submitted = False
        self.outcome: Optional[str] = None
        self.idempotency_key: Optional[str] = None
        self.before: Set[str] = set()
        self.pod_ids: Set[str] = set()
        self.removed: Set[str] = set()
        self.candidates: List[str] = []
        self.last_error: Optional[str] = None
        self.replay: Optional[LiumRentOutcomeUnknownError] = None
        self.submitted_at: Optional[float] = None
        self.scope: Optional[Dict[str, str]] = None
        self.recovered = False
        self.handed: List["_Rental"] = []
        self.reported = False  # this call's own result names its pod
        self.delivered = False  # a result naming the pod was handed to the transport; the host may not have it
        self.delivered_at: Optional[float] = None  # time.monotonic() of that hand-over
        self.held_elsewhere = False  # read from another live session's journal: never settled here
        # time.monotonic() at which a listing started after the hand-over last showed the pod
        self.listed_at: Optional[float] = None

    def state(self) -> Dict[str, Any]:
        """What a restarted server needs to finish this rental: the request to replay, the account it was sent
        for, when, and whether the host still wants its pod."""
        with self.lock:
            error = self.replay
            return {
                "idempotency_key": self.idempotency_key,
                "name": self.name,
                "executor_id": error.executor_id if error else None,
                "endpoint": error.endpoint if error else None,
                "payload": error.payload if error else None,
                "submitted_at": self.submitted_at,
                "scope": self.scope,
                "cancelled": self.cancelled,
                "before": sorted(self.before),
                "pod_ids": sorted(self.pod_ids),
                "delivered": self.delivered,
            }

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "_Rental":
        rental = cls()
        rental.name, rental.cancelled, rental.submitted = state["name"], bool(state.get("cancelled")), True
        rental.outcome, rental.idempotency_key = "unknown", state["idempotency_key"]
        rental.before = set(state.get("before") or [])
        rental.submitted_at, rental.scope = state.get("submitted_at"), state.get("scope")
        rental.replay = LiumRentOutcomeUnknownError(
            "restored from an earlier session", idempotency_key=state["idempotency_key"], pod_name=state["name"],
            executor_id=state.get("executor_id"), endpoint=state.get("endpoint"), payload=state.get("payload"),
        )
        pod_ids = state.get("pod_ids") or []
        if pod_ids:  # the API named the pod before the restart: remove it (cancelled) or hand it over, no replay needed
            rental.outcome, rental.pod_ids = "created", set(pod_ids)
        rental.delivered = bool(state.get("delivered"))
        return rental

    def report(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "name": self.name,
                "outcome": self.outcome,
                "pod_ids": sorted(self.pod_ids),
                "same_name_pods_to_check": list(self.candidates),
                "idempotency_key": self.idempotency_key,
                "last_error": self.last_error,
            }


def _try_lock(fd: int) -> bool:
    """A non-blocking exclusive lock on ``fd``, held until it is closed; False while another holds it."""
    try:
        try:
            import fcntl
        except ImportError:  # Windows: a non-blocking lock on the file's first byte, held as long
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


class _RentalStore:
    """Rentals whose outcome the host has not received, on disk, one file per idempotency key. A request is
    written before it is sent, so a server that stops mid-request replays the same key after a restart instead
    of forgetting it: a new key would let the API rent a second pod for the one the first request made."""

    def __init__(self, directory: Optional[Path]):
        self.directory = directory
        self._lock = threading.Lock()
        # open fds of the "<key>.lock" files this server holds an flock on: every `lium mcp` process shares the
        # directory (one per Claude Code session, Desktop, Cursor), and each owns only the journals it locked
        self._held: Dict[str, int] = {}

    def _claim(self, key: str) -> bool:
        """Take the journal of ``key`` for this server; False while another live server holds it. The OS drops
        the lock when its holder exits, so a crashed server's journals are adopted by the next one to start."""
        if key in self._held:
            return True
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.directory / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        if not _try_lock(fd):
            os.close(fd)
            return False
        self._held[key] = fd
        return True

    def _release(self, key: str) -> None:
        fd = self._held.pop(key, None)
        if fd is not None:
            os.close(fd)  # drops the lock on either OS; Windows cannot unlink a file still open
            # a server that locked the path meanwhile finds no journal and releases it; this unlink is then harmless,
            # and on Windows it fails while that server has the file open: the lock file is left, never the journal
            try:
                (self.directory / f"{key}.lock").unlink(missing_ok=True)
            except OSError:
                pass  # only the empty lock file is left; the journal is already gone, so nothing re-adopts it

    def path(self, rental: _Rental) -> Optional[Path]:
        return self.directory / f"{rental.idempotency_key}.json" if self.directory and rental.idempotency_key else None

    def pending(self) -> bool:
        return self.directory is not None and any(self.directory.glob("*.json"))

    def load(self, scope: Dict[str, str]) -> List[_Rental]:
        """Take the journals sent for ``scope`` (API and account) that no live server holds. Others are left
        untouched and unlocked: the same key is a new rent under another account, so only its own server settles
        it."""
        if self.directory is None or not self.directory.is_dir():
            return []
        rentals = []
        for path in sorted(self.directory.glob("*.json")):
            with self._lock:
                try:
                    state = json.loads(path.read_text())
                    if state.get("scope") != scope:
                        continue
                except (OSError, ValueError, AttributeError):
                    continue  # gone meanwhile, or unreadable: left in place for the user, never deleted
                if path.stem in self._held:
                    continue  # already this server's: tracked in one of its lists, never loaded twice
                if not self._claim(path.stem):
                    continue  # another running server's rental: settled and reported by that server, not here
                try:  # read again under the lock: the holder may have rewritten or settled it meanwhile
                    rentals.append(_Rental.from_state(json.loads(path.read_text())))
                except (OSError, ValueError, KeyError, TypeError):
                    self._release(path.stem)
        return rentals

    def lock_admission(self, scope: Dict[str, str], timeout: float) -> Optional[int]:
        """Take this account's rent-admission lock, shared by every server on this machine, within ``timeout``
        seconds; its fd (close it to release), -1 with no journal directory, None while another server holds it.
        Held from the admission check until the rent's journal is written: two servers starting a rent together
        cannot both find no journal and both send."""
        if self.directory is None:
            return -1
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        tag = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:16]
        fd = os.open(self.directory / f"admit-{tag}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        stop = time.monotonic() + max(0.0, timeout)
        while not _try_lock(fd):
            if time.monotonic() >= stop:
                os.close(fd)
                return None
            time.sleep(0.05)
        return fd

    def journals_elsewhere(self, scope: Dict[str, str]) -> List[_Rental]:
        """This account's journals, read now, that this server does not hold: another running server's rent whose
        outcome is unknown or whose pod its host has not acknowledged (or an orphan not adopted yet). Never claimed,
        adopted or replayed here, but rent_pod is refused beside them: a retry here would be a second pod."""
        if self.directory is None or not self.directory.is_dir():
            return []
        found = []
        for path in sorted(self.directory.glob("*.json")):
            with self._lock:
                if path.stem in self._held:
                    continue
            try:
                state = json.loads(path.read_text())
                if state.get("scope") != scope:
                    continue
                other = _Rental.from_state(state)
            except (OSError, ValueError, AttributeError, KeyError, TypeError):
                continue  # gone meanwhile (settled), or unreadable
            other.last_error = (
                f"another running Lium MCP session rented pod(s) {', '.join(sorted(other.pod_ids))} and has not yet "
                "confirmed that its host received them (if you know them, pass them in known_pod_ids to rent another "
                "pod; do not remove_pod them, they are that session's)" if other.pod_ids else
                "another running Lium MCP session sent this rent and is settling it"
            ) + "; rent_pod is refused here until it does or that session ends (this server then takes it over)"
            other.held_elsewhere = True
            found.append(other)
        return found

    def save(self, rental: _Rental) -> None:
        """Raises if the record cannot be written."""
        # one writer at a time, each reading the rental only once it holds the lock: a cancellation and the worker
        # that just learned the pod id both save, and the record on disk must end as the newer of the two, whole
        with self._lock:
            state, path = rental.state(), self.path(rental)
            if path is None or not state["endpoint"]:
                return
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._claim(path.stem)  # a fresh key: no other server has it
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f"{path.stem}.", suffix=".tmp")  # 0600, its own
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(state, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    def journaled(self, rental: _Rental) -> bool:
        """Whether ``rental`` has a journal on disk at all (save() writes none for a request it cannot replay)."""
        with rental.lock:
            replay = rental.replay
        return self.path(rental) is not None and replay is not None and bool(replay.endpoint)

    def forget(self, rental: _Rental) -> None:
        path = self.path(rental)
        if path is not None:
            with self._lock:
                path.unlink(missing_ok=True)
                self._release(path.stem)


DEFAULT_RENTAL_STATE_DIR = Path.home() / ".lium" / "mcp-pending-rentals"
# The API keeps a completed idempotency record for 24 hours; after that the same key is a new rent. Replays stop an
# hour before, and the rental stays unresolved.
REPLAY_WINDOW_SECONDS = 23 * 3600
# The background reconciler replays a wanted rental only this soon after it was sent (past the API's 300 s in-flight
# lock). Later, a replay that rents is a pod nobody is told of, so it happens only in rent_pod/list_pods, whose
# result names the pod at once.
BACKGROUND_REPLAY_SECONDS = 600


def _unlisted_pod(pod_id: str, name: str) -> PodInfo:
    """A pod the API named that the listing does not show (yet): enough of it to delete it by id."""
    return PodInfo(
        id=pod_id, name=name, status="UNKNOWN", huid="", ssh_cmd=None, ports={}, created_at="", updated_at="",
        executor=None, template={}, removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )


def _tail(text: Optional[str]) -> str:
    text = text or ""
    return text if len(text) <= OUTPUT_TAIL_CHARS else f"…[cut to the last {OUTPUT_TAIL_CHARS} characters]\n" + text[-OUTPUT_TAIL_CHARS:]


class LiumTools:
    """One Lium client for the whole session, built on first use so `lium mcp` starts even without a key."""

    def __init__(
        self,
        client_factory: Callable[[], Lium] = lambda: Lium(source="mcp"),
        state_dir: Optional[Path] = None,
    ):
        self._factory = client_factory
        self._client: Optional[Lium] = None
        # Rentals whose result the host never got (it cancelled, or the rent call went unanswered). Each stays here
        # until its pod is confirmed removed, confirmed never created, or recovered and handed to the host; while
        # any does, rent_pod refuses to rent. They are also kept on disk, so a restart replays rather than forgets.
        self._store = _RentalStore(state_dir or DEFAULT_RENTAL_STATE_DIR)
        self._cleanup = threading.Condition()
        # restored rentals start unresolved; they are adopted (_adopt) once the account is known, which needs the
        # client, so `lium mcp` still starts without a key
        self._unresolved: List[_Rental] = []
        self._adopting = threading.Lock()
        self._recovered: List[_Rental] = []  # known pods the host has not been told of
        # pods a result named that the host has not yet shown it knows (it names the pod in a call, or the pod is
        # gone): a returned callback is not a written response, so their journals stay until then
        self._delivered: List[_Rental] = []
        # Every submitted rental the host has not acknowledged is in one of these lists at every moment, and moves
        # between them only under _cleanup, so admission never sees a gap. _in_flight: rent_pod calls admitted and
        # not yet finished (one at a time). _handing: recovered rentals reserved for a call whose result is on
        # its way to the host.
        self._in_flight: List[_Rental] = []
        self._handing: List[_Rental] = []
        self._reconciling = threading.Lock()
        self._reconciler_running = False
        if self._store.pending():
            # a restored rental may be billing: settle it now, not at the first tool call, which may never come
            self._start_reconciler()

    @property
    def client(self) -> Lium:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def _find_pod(self, pod: str) -> PodInfo:
        """The one pod `pod` names. An exact id wins; a huid or name must match exactly one pod, so the target
        never depends on listing order (names are free text and need not be unique)."""
        pods = self.client.ps()
        for candidate in pods:
            if candidate.id == pod:
                return self._acknowledged(candidate)
        matches = {c.id: c for c in pods if pod in (c.huid, c.name)}
        if len(matches) == 1:
            return self._acknowledged(next(iter(matches.values())))
        if matches:
            raise LookupError(
                f"'{pod}' matches {len(matches)} pods ({', '.join(sorted(matches))}); pass the pod_id of the one you mean."
            )
        raise LookupError(f"No pod '{pod}' among your {len(pods)} pods; call list_pods for their ids and names.")

    def list_gpus(self, gpu_type: Optional[str] = None, gpu_count: Optional[int] = None, limit: int = 20) -> Dict[str, Any]:
        def call():
            nodes = self.client.ls(gpu_type=gpu_type, gpu_count=gpu_count)
            nodes = sorted(nodes, key=lambda n: n.price_per_gpu)[: max(1, limit)]
            return {"ok": True, "billing": "per second at the hourly price, no minimum", "nodes": [_node(n) for n in nodes]}

        return _guarded(call)

    CLEANUP_WAIT_SECONDS = 30  # how long rent_pod waits for an earlier rental's cleanup before refusing
    CLEANUP_RETRY_SECONDS = 2.0  # first background retry; doubles up to CLEANUP_RETRY_MAX_SECONDS
    CLEANUP_RETRY_MAX_SECONDS = 30.0
    BACKGROUND_CLEANUP = True

    def rent_pod(
        self,
        executor_id: str,
        name: str,
        template_id: Optional[str] = None,
        timeout_seconds: int = 600,
        rental: Optional[_Rental] = None,
        known_pod_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """``known_pod_ids``: pods the host says it already holds; naming one acknowledges its delivery.
        ``rental``: the async tool passes its own and calls :meth:`deliver` or :meth:`cancel_rental` once it knows
        whether the result reached the host; a direct caller gets the result here, so it is delivered on return."""
        direct = rental is None
        rental = rental or _Rental()
        not_rented = _error("cancelled", "The rental was cancelled before it was sent; nothing was rented.")

        admission: List[int] = []  # the account's cross-server admission lock, held until the journal is written

        def release_admission() -> None:
            while admission:
                fd = admission.pop()
                if fd >= 0:
                    os.close(fd)

        def call():
            try:
                return admit_and_rent()
            finally:
                release_admission()

        def admit_and_rent():
            deadline = time.monotonic() + self.CLEANUP_WAIT_SECONDS
            while True:
                self._reconcile(reports=True)
                # a pod already returned whose result the host may have dropped (a timeout, a lost response) is
                # returned again rather than rented again, until the host names it
                self._forget_delivered(lambda pod_ids: bool(pod_ids & set(known_pod_ids or [])))
                try:
                    scope = self._scope()
                except Exception:  # noqa: BLE001 - no client yet: the rent below fails the same way, sending nothing
                    fd = -1
                else:
                    fd = self._store.lock_admission(scope, max(0.0, deadline - time.monotonic()))
                if fd is None:
                    return self._admission_busy_error()
                admission.append(fd)
                admitted = False
                with self._cleanup:
                    # one look at every list, and the admission itself, under the lock the lists change under: two
                    # calls cannot both find nothing and both rent
                    recovered = self._hand_off_recovered(rental)
                    recovered += self._unacknowledged(exclude=rental.handed)
                    # read now, not at the last reconcile: another server may have sent a rent since
                    # another session's pod the host names here is known: renting beside it is what the host asks
                    unresolved = list(self._unresolved) + [
                        r for r in self._journals_elsewhere() if not r.pod_ids & set(known_pod_ids or [])]
                    renting = [r for r in self._in_flight if r is not rental]
                    if not (recovered or unresolved or renting or self._recovered or rental.cancelled):
                        self._in_flight.append(rental)
                        admitted = True
                if admitted:
                    break  # the admission lock stays held until this rent's journal is written
                release_admission()
                if recovered:
                    # an earlier rent the host never heard back from did rent: that is the answer, not a new rent
                    return self._recovered_result(recovered)
                if rental.cancelled:
                    return not_rented
                remaining = deadline - time.monotonic()
                started = renting and not unresolved and all(r.pod_ids for r in renting)
                if remaining <= 0 or started:
                    # a rent_pod call still waiting for its pod to start will not finish soon: say so now
                    if renting and not unresolved:
                        return self._rent_in_progress_error(renting)
                    return self._cleanup_pending_error(unresolved)
                with self._cleanup:
                    if not rental.cancelled:
                        self._cleanup.wait(min(remaining, self.CLEANUP_RETRY_SECONDS))

            try:
                return rent()
            finally:
                with rental.lock:
                    reported = rental.reported
                if not reported:
                    # nothing to deliver: rejected, never sent, or unknown (tracked as unresolved before this). A
                    # result that names the pod keeps its place until deliver or cancel_rental moves it
                    self._release(rental)

        def rent():
            before = {p.id for p in self.client.ps()}
            key = str(uuid.uuid4())
            with rental.lock:
                if rental.cancelled:
                    return not_rented
                rental.name, rental.before, rental.submitted, rental.idempotency_key = name, before, True, key

            def journal(request: Dict[str, Any]) -> None:
                # written before the first request leaves: if this server stops mid-request, the next one replays
                # this key. If it cannot be written, up() sends nothing
                with rental.lock:
                    rental.replay = LiumRentOutcomeUnknownError(
                        "sent; outcome not yet known", idempotency_key=request["idempotency_key"], pod_name=name,
                        executor_id=executor_id, endpoint=request["endpoint"], payload=request["payload"],
                    )
                    rental.submitted_at, rental.scope = time.time(), self._scope()
                self._store.save(rental)
                release_admission()  # on disk now: another server's admission sees it

            # rent and wait separately: once the rent call returns the pod bills, so a slow start must hand back its id
            # rather than an error the model would answer by renting a second pod. confirmed_only: the SDK never
            # hands back a pod found by name, which may be another client's; it reports the outcome as unknown.
            try:
                created = self.client.up(
                    executor_id=executor_id, name=name, template_id=template_id, wait=False,
                    idempotency_key=key, confirmed_only=True, before_send=journal,
                )
            except LiumRentOutcomeUnknownError as e:
                # a request went unanswered and nothing proved what became of it: a pod may exist. The journaled
                # request is replayed with the same key until the API answers
                self._settle(rental, "unknown", idempotency_key=e.idempotency_key or key)
                with rental.lock:
                    rental.candidates = list(e.candidates)
                self._track(rental)
                return self._unknown_outcome_error(name, e)
            except Exception:
                # every other failure is one the SDK raises only when no request could have rented: a check before
                # the request (a journal that cannot be written included), or the API's refusal of every request
                self._settle(rental, "rejected", idempotency_key=key)
                self._store.forget(rental)
                raise

            pod_id = created.get("id") if isinstance(created, dict) else getattr(created, "id", None)
            self._settle(rental, "created" if pod_id else "unknown", pod_id)
            if not pod_id:
                self._track(rental)
                return self._unknown_outcome_error(name, ValueError("the rent call returned no pod id"))
            with rental.lock:
                cancelled = rental.cancelled
            if cancelled:
                self._track(rental)  # cancelled while up() was in flight: its pod id is known only now
            else:
                # the journal now names the pod, and stays until the result reaches the host (deliver): a server that
                # stops during the wait below hands this pod over after a restart instead of renting another
                self._save(rental)
            with rental.lock:
                rental.reported = True  # every result from here on names pod_id
            pending = {
                "ok": True,
                "status": "pending",
                "pod_id": pod_id,
                "name": name,
                "next": "The pod is rented and billing. Do NOT call rent_pod again: call list_pods until its status is "
                "RUNNING, then run_command; or remove_pod with this pod_id to stop billing.",
            }

            def stop_if_cancelled(*_):
                if rental.cancelled:
                    raise _RentalCancelled()

            try:
                stop_if_cancelled()
                ready = self.client.wait_ready(created, timeout=timeout_seconds, on_poll=stop_if_cancelled)
            except _RentalCancelled:
                return _error("cancelled", f"The rental was cancelled; pod {pod_id} is being removed.")
            except PodStartError as e:
                failed = _error(
                    "pod_start_failed",
                    f"Pod {pod_id} will not start (status {e.status}): {e}",
                    "Call remove_pod with this pod_id so it cannot bill, then rent a different node.",
                )
                failed["pod_id"] = pod_id
                return failed
            except Exception as e:  # noqa: BLE001 - the rent went through; losing its id would cause a second rental
                return {**pending, "warning": f"Could not confirm the pod is ready: {type(e).__name__}: {e}"}
            if ready is None:
                return {**pending, "warning": f"Not running after {timeout_seconds}s; some nodes take longer to start."}
            return {"ok": True, "pod": _pod(ready), "next": "run_command on this pod; remove_pod when done (billing stops)"}

        result = _guarded(call)
        if direct:
            self.deliver(rental)
        return result

    def _release(self, rental: _Rental) -> None:
        with self._cleanup:
            if rental in self._in_flight:
                self._in_flight.remove(rental)
            self._cleanup.notify_all()

    def deliver(self, rental: _Rental) -> None:
        """The call ``rental`` returned its result to the MCP transport. That is not proof the host read it (the
        server can die before the response is written), so the pods it named stay journaled, marked delivered, until
        the host names one in a call or the pod is gone. A restart before then hands them over once more."""
        with self._cleanup:
            handed, rental.handed = rental.handed, []
            handed = [r for r in handed if r in self._handing]  # one cancelled meanwhile is being removed instead
            with rental.lock:
                # only a result that names the pod: an unknown-outcome error recovered in the background since does not
                own = rental.reported and not rental.cancelled and rental in self._in_flight
            reached = handed + ([rental] if own else [])
            # one step under the lock: a rental leaves _handing or _in_flight only as it enters _delivered
            self._handing = [r for r in self._handing if r not in handed]
            self._in_flight = [r for r in self._in_flight if r is not rental]
            for target in reached:
                with target.lock:
                    target.delivered, target.delivered_at, target.listed_at = True, time.monotonic(), None
                if target not in self._delivered:
                    self._delivered.append(target)
            self._cleanup.notify_all()
        for target in reached:
            self._save(target)

    def _acknowledged(self, pod: PodInfo) -> PodInfo:
        """The host named ``pod`` in a call, so it knows the pod: a journal kept for its delivery can go."""
        self._forget_delivered(lambda pod_ids: pod.id in pod_ids)
        return pod

    def _forget_delivered(self, done: Callable[[Set[str]], bool]) -> None:
        with self._cleanup:
            finished = [r for r in self._delivered if done(set(r.pod_ids))]
            self._delivered = [r for r in self._delivered if r not in finished]
        for rental in finished:
            self._store.forget(rental)

    def _forget_gone(self, live: Set[str], listing_started: float) -> None:
        """Drop delivered pods that a listing shows are gone: nothing is left to tell the host about. Only a listing
        that started after the delivery counts, and only one that also started after a listing showed the pod: an
        older snapshot, or one the new pod is not in yet, says nothing about it. Listings overlap, so the order
        they return in is not the order they were taken in."""
        with self._cleanup:
            finished = []
            for rental in self._delivered:
                if rental.delivered_at is None or rental.delivered_at >= listing_started:
                    continue
                if rental.pod_ids & live:
                    rental.listed_at = time.monotonic()
                elif rental.listed_at is not None and rental.listed_at < listing_started:
                    finished.append(rental)
            self._delivered = [r for r in self._delivered if r not in finished]
        for rental in finished:
            self._store.forget(rental)

    def _unacknowledged(self, exclude: List[_Rental]) -> List[Dict[str, Any]]:
        with self._cleanup:
            # _handing: another call is reporting it right now; until the host names the pod it is not known to
            return [{**self._recovered_report(r), "may_have_been_reported": True}
                    for r in self._handing + self._delivered if r not in exclude]

    @staticmethod
    def _recovered_report(rental: _Rental) -> Dict[str, Any]:
        return {
            "name": rental.name, "pod_ids": sorted(rental.pod_ids), "idempotency_key": rental.idempotency_key,
            **({"may_have_been_reported": True} if rental.delivered else {}),
        }

    def cancel_rental(self, rental: _Rental) -> None:
        """The host cancelled the call and will never see its result. A rental not yet sent stops before renting; a sent
        one is tracked until its pod is confirmed removed, whatever stage the worker thread has reached. Recovered
        pods the call was about to hand over wait for the next call."""
        with rental.lock:
            rental.cancelled = True
            submitted = rental.submitted
        with self._cleanup:
            # one step under the lock: a reserved rental leaves _handing only as it re-enters _recovered
            handed, rental.handed = [r for r in rental.handed if r in self._handing], []
            self._handing = [r for r in self._handing if r not in handed]
            self._recovered.extend(r for r in handed if r not in self._recovered)
        if submitted:
            self._track(rental)
        with self._cleanup:
            self._cleanup.notify_all()

    @staticmethod
    def _settle(rental: _Rental, outcome: str, pod_id: Optional[str] = None, idempotency_key: Optional[str] = None) -> None:
        with rental.lock:
            rental.outcome = outcome
            rental.idempotency_key = idempotency_key or rental.idempotency_key
            if pod_id:
                rental.pod_ids.add(pod_id)

    def _scope(self) -> Dict[str, str]:
        """The API and account a request is sent for, without the key itself: the API keeps idempotency keys per
        account, so a replay under another account would be a new rent."""
        client = self.client
        config = getattr(client, "config", None)
        key = (getattr(client, "headers", None) or {}).get("X-API-KEY") or ""
        # an account fingerprint, not a stored secret: the key is the HMAC key of a fixed label, so the journal holds
        # a short tag that tells two keys apart and says nothing about either
        tag = hmac.new(key.encode(), b"lium-mcp-rental-scope", hashlib.sha256).hexdigest()[:16]
        return {"base_url": str(getattr(config, "base_url", "")), "account_tag": tag}

    def _save(self, rental: _Rental) -> None:
        try:
            self._store.save(rental)
        except OSError as e:  # the in-memory guard already holds; only a restart would lose it
            with rental.lock:
                rental.last_error = f"could not save this rental to {self._store.directory}: {e}"

    def _track(self, rental: _Rental) -> None:
        """Keep ``rental`` until it is settled: in memory first, then on disk."""
        with self._cleanup:
            if rental in self._recovered:
                # cancelled after a replay kept its pod and before the host was told: remove the pod after all
                self._recovered.remove(rental)
                with rental.lock:
                    rental.recovered = False
            if rental not in self._unresolved:
                self._unresolved.append(rental)
            # only now does it leave the lists that held it, under the same lock
            self._handing = [r for r in self._handing if r is not rental]
            self._in_flight = [r for r in self._in_flight if r is not rental]
        self._save(rental)
        self._start_reconciler()

    def _start_reconciler(self) -> None:
        with self._cleanup:
            start = self.BACKGROUND_CLEANUP and not self._reconciler_running
            self._reconciler_running = self._reconciler_running or start
        if start:
            threading.Thread(target=self._reconcile_in_background, daemon=True).start()

    def _reconcile_in_background(self) -> None:
        """Retry removals and replays until nothing is unresolved."""
        delay = self.CLEANUP_RETRY_SECONDS
        while True:
            try:
                self._reconcile()
            except Exception:  # noqa: BLE001 - this thread is the only one that retries; it must outlive any one pass
                pass
            with self._cleanup:
                # read under the lock _track takes: a rental tracked after _reconcile's snapshot is seen here, or
                # _track sees this thread still running and starts none, and it would wait for the next rent_pod
                if not self._unresolved:
                    self._reconciler_running = False
                    return
            time.sleep(delay)
            delay = min(delay * 2, self.CLEANUP_RETRY_MAX_SECONDS)

    def _reconcile(self, reports: bool = False) -> List[_Rental]:
        """Try once to settle every unresolved rental; returns those still unresolved. ``reports``: the caller's
        result hands recovered pods to the host, so a wanted rental may be replayed however old it is."""
        self._adopt()
        with self._cleanup:
            if not self._unresolved:
                return []
        with self._reconciling:
            with self._cleanup:
                pending = list(self._unresolved)
            try:
                live = {p.id: p for p in self.client.ps()}
            except Exception as e:  # noqa: BLE001 - nothing is confirmed; try again later
                for rental in pending:
                    with rental.lock:
                        rental.last_error = f"listing pods failed: {type(e).__name__}: {e}"
                return pending
            settled = {rental: self._settle_cleanup(rental, live, reports) for rental in pending}
            with self._cleanup:
                for rental, disposition in settled.items():
                    if disposition == "pending" or rental not in self._unresolved:
                        continue
                    with rental.lock:
                        if rental.cancelled and disposition == "keep":
                            # cancelled while the replay settled it: the pod is removed, not kept
                            rental.recovered = False
                            continue
                        rental.recovered = disposition == "keep"
                    self._unresolved.remove(rental)
                    if disposition == "keep":
                        self._recovered.append(rental)
                    else:
                        self._store.forget(rental)
                self._cleanup.notify_all()
                return list(self._unresolved)

    def _journals_elsewhere(self) -> List[_Rental]:
        try:
            scope = self._scope()
        except Exception:  # noqa: BLE001 - no client yet (e.g. no API key): nothing can be rented anyway
            return []
        return self._store.journals_elsewhere(scope)

    def _adopt(self) -> None:
        """Take this account's journals that no live server holds, before any rental is admitted (rent_pod
        reconciles first). Runs on every reconcile, not once: a predecessor still finishing its last rent holds its
        journal when this server starts, and a long-lived server must adopt another session's orphan when that
        session exits."""
        with self._adopting:
            try:
                scope = self._scope()
            except Exception:  # noqa: BLE001 - no client yet (e.g. no API key): nothing can be rented; try at the next call
                return
            loaded = self._store.load(scope)
            with self._cleanup:
                self._unresolved.extend(r for r in loaded if r not in self._unresolved)
        if loaded:
            self._start_reconciler()  # an orphan adopted by a tool call keeps being retried, not only at the next call

    def _settle_cleanup(self, rental: _Rental, live: Dict[str, PodInfo], reports: bool = False) -> str:
        """How far this rental is settled: "done" once nothing it made can still bill (the SDK established that it
        rented nothing, or the API acknowledged the removal of every pod it named for a cancelled rental, or answered
        that the pod does not exist); "keep" once a replay named the pod of a rental the host still wants; else
        "pending". Elapsed time settles nothing, and a pod is never removed because of its name."""
        with rental.lock:
            outcome = rental.outcome
            if outcome is None:
                rental.last_error = "the rent call has not returned yet"
                return "pending"
            if outcome == "rejected":
                return "done"
            scope = rental.scope
        if self._store.journaled(rental) and not self._store.path(rental).exists():
            # a deleted journal settles nothing (only the API's answer or the removal of the rental's pods does):
            # keep blocking, and on disk for a restart
            try:
                self._store.save(rental)
            except Exception:  # noqa: BLE001 - still blocked in memory; the next pass writes it again
                pass
        if scope is not None and scope != self._scope():
            # restored from another API or account: neither replayed, removed nor handed over here
            with rental.lock:
                rental.last_error = (f"this rental was sent for another API or account than this server uses now; "
                                     f"it is left for that account (journal {self._store.path(rental)})")
            return "pending"
        with rental.lock:
            if outcome == "unknown":
                rental.candidates = sorted(p.id for p in live.values() if p.name == rental.name and p.id not in rental.before)
        if outcome == "unknown":
            with rental.lock:
                late = not rental.cancelled and (
                    rental.submitted_at is None or time.time() - rental.submitted_at > BACKGROUND_REPLAY_SECONDS)
                if late and not reports:
                    rental.last_error = ("not replayed in the background: the rent was sent too long ago for a pod "
                                         "it makes to be noticed; the next rent_pod or list_pods replays it and "
                                         "reports the pod")
            if (late and not reports) or not self._replay(rental):
                return "pending"
        with rental.lock:
            if rental.outcome == "rejected":
                return "done"
            keep = not rental.cancelled
            if keep:
                rental.recovered = True
            pod_ids = set(rental.pod_ids)
        if keep:
            self._save(rental)  # written before the hand-off can see it: a restart in between must still tell the host
            return "keep"
        for pod_id in pod_ids:
            # a pod the listing does not show yet is still removed by its id: the API named it, so it exists
            target = live.get(pod_id) or _unlisted_pod(pod_id, rental.name)
            try:
                self.client.rm(target)
            except LiumNotFoundError:
                pass  # the API says there is no such pod: nothing left to bill
            except Exception as e:  # noqa: BLE001 - e.g. the API refuses to delete a pod still PENDING; retry later
                with rental.lock:
                    rental.last_error = f"removing {pod_id} failed: {type(e).__name__}: {e}"
                continue
            with rental.lock:
                rental.pod_ids.discard(pod_id)
                rental.removed.add(pod_id)
        with rental.lock:
            return "pending" if rental.pod_ids else "done"

    def _replay_blocker(self, rental: _Rental) -> Optional[str]:
        """Why ``rental`` cannot be replayed with its key, or None when a replay can still get the API's answer."""
        with rental.lock:
            error, scope, submitted_at = rental.replay, rental.scope, rental.submitted_at
        if error is None or not error.endpoint:
            return "the rent call's request was not kept, so it cannot be replayed"
        if scope != self._scope():
            return ("this rental was sent for another API or account than this server uses now; it is not replayed "
                    "here (the same key would be a new rent there)")
        if submitted_at is None or time.time() - submitted_at > REPLAY_WINDOW_SECONDS:
            return "this rental is older than the API keeps idempotency keys, so a replay could rent a second pod"
        if not self.client.supports("rent_idempotency"):
            return ("the API does not advertise idempotent rents (rent_idempotency), so a replay could rent a "
                    "second pod")
        return None

    def _replay(self, rental: _Rental) -> bool:
        """Send an unknown rental's request again with its key; True once the API answered it (rental settled).

        A replay is sent only when the same key is still bound to the same first request: same API and account
        (the API keeps keys per account), within the API's retention of the key, on an API that advertises
        idempotent rents. Otherwise the rental stays unresolved for the user to settle."""
        with rental.lock:
            error = rental.replay

        def unresolved(reason: str) -> bool:
            with rental.lock:
                rental.last_error = reason
            return False

        # nothing can establish what an unanswered, unreplayable rent did, so no second rent is allowed beside it
        # (any other client sends a new key, so pointing there would bypass this guard)
        manual = ("; this server cannot settle it, so rent_pod stays refused while it is listed here (deleting its "
                  "journal does not lift that). Do not rent through any client (this server, the lium CLI or the "
                  "website) until this rental is resolved: if the unanswered request completes, a second rental "
                  "would bill too. Check list_pods and same_name_pods_to_check and remove_pod what you do not want")
        blocker = self._replay_blocker(rental)
        if blocker:
            return unresolved(blocker + manual)
        try:
            created = self.client.replay_rent(error)
        except LiumRentOutcomeUnknownError as e:
            return unresolved(f"replaying the rent with the same key did not establish its outcome yet: {e}")
        except LiumError:
            # replay_rent raises a plain LiumError only for the API's stored refusal of the first request
            with rental.lock:
                rental.outcome = "rejected"
            return True
        except Exception as e:  # noqa: BLE001 - nothing is established; try again later
            return unresolved(f"replaying the rent failed: {type(e).__name__}: {e}")
        with rental.lock:
            rental.outcome = "created"
            rental.pod_ids.add(created["id"])
        return True

    def _hand_off_recovered(self, receiver: _Rental) -> List[Dict[str, Any]]:
        """Reserve the recovered rentals the host has not been told of for the call ``receiver``. Their journals stay
        until that call's result reaches the host (:meth:`deliver`); if it is cancelled they wait for the next call."""
        with self._cleanup:
            with receiver.lock:
                if receiver.cancelled:
                    # cancelled already: nothing would deliver or requeue what it took, so it takes nothing
                    return []
            delivered = [rental for rental in self._recovered if not rental.cancelled]
            self._recovered = [rental for rental in self._recovered if rental not in delivered]
            # reserved, and still in a list admission reads: a rent_pod running beside this call sees them
            self._handing.extend(delivered)
            receiver.handed.extend(delivered)
        return [self._recovered_report(rental) for rental in delivered]

    @staticmethod
    def _recovered_result(recovered: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "ok": True,
            "status": "recovered",
            "recovered_rentals": recovered,
            "next": "Nothing new was rented. Earlier rent_pod calls did rent these pods and they are billing; their "
            "result may not have reached you. Use their pod_ids (list_pods for status, run_command once RUNNING) or "
            "remove_pod them. If you need another pod as well, call rent_pod again with known_pod_ids set to these "
            "pod ids.",
        }

    def _cleanup_pending_error(self, unresolved: List[_Rental]) -> Dict[str, Any]:
        unknown = any(rental.outcome == "unknown" for rental in unresolved)
        failed = _error(
            "cleanup_pending",
            f"{len(unresolved)} earlier rental(s) whose result never reached you may still be billing; renting again "
            "is refused until each one is settled.",
            "An earlier rent call got no answer. It is re-sent with the same idempotency key while the original "
            "account's key is unexpired; then rent_pod returns that pod instead of renting. Retry in a minute; if "
            "last_error says it cannot be replayed, follow it."
            if unknown
            else "Another Lium MCP session holds these pods: if you know them, call rent_pod again with known_pod_ids "
            "set to their ids; do not remove_pod them."
            if all(rental.held_elsewhere for rental in unresolved)
            else "Retry rent_pod in a minute, or call list_pods and remove_pod the pod ids listed under unresolved.",
        )
        failed["unresolved"] = [rental.report() for rental in unresolved]
        return failed

    @staticmethod
    def _admission_busy_error() -> Dict[str, Any]:
        return _error(
            "rent_in_progress",
            "Another Lium MCP session on this machine is starting a rent for this account; nothing new was rented.",
            "Retry rent_pod in a minute: it then sees that session's rental and does not rent beside it unasked.",
        )

    @staticmethod
    def _rent_in_progress_error(renting: List[_Rental]) -> Dict[str, Any]:
        failed = _error(
            "rent_in_progress",
            f"{len(renting)} earlier rent_pod call(s) are still running; nothing new was rented.",
            "Wait for that call's result instead of renting again: a pod listed under in_progress is already rented "
            "and billing. If you no longer want it, cancel that call or remove_pod the pod. To rent another pod as "
            "well, call rent_pod again once that call has returned, with known_pod_ids set to its pod id.",
        )
        failed["in_progress"] = [rental.report() for rental in renting]
        return failed

    @staticmethod
    def _unknown_outcome_error(name: str, e: Exception) -> Dict[str, Any]:
        return _error(
            "rent_outcome_unknown",
            f"The rent call got no clear answer ({type(e).__name__}: {e}); a pod named '{name}' may have been created.",
            "Do NOT rent again. Automatic replay requires the original account and an unexpired idempotency record; "
            "with those, the call is re-sent with the same key, and the next rent_pod or list_pods reports the pod "
            "under recovered_rentals. Otherwise the rental stays under unresolved_rentals, and rent_pod is refused "
            "until it is settled.",
        )

    def list_pods(self, receiver: Optional[_Rental] = None) -> Dict[str, Any]:
        """``receiver``: as ``rental`` in :meth:`rent_pod`, for the recovered pods this listing hands over."""
        direct = receiver is None
        receiver = receiver or _Rental()

        def call():
            listing_started = time.monotonic()
            pods = self.client.ps()
            self._forget_gone({p.id for p in pods}, listing_started)
            result = {"ok": True, "pods": [_pod(p) for p in pods]}
            unresolved = self._reconcile(reports=True)
            if unresolved:
                result["unresolved_rentals"] = [rental.report() for rental in unresolved]
            recovered = self._hand_off_recovered(receiver)
            if recovered:
                result["recovered_rentals"] = recovered
            return result

        result = _guarded(call)
        if direct:
            self.deliver(receiver)
        return result

    def run_command(self, pod: str, command: str, timeout_seconds: int = 300) -> Dict[str, Any]:
        def call():
            target = self._find_pod(pod)
            try:
                result = self.client.exec(
                    target, command=command, timeout=timeout_seconds, max_output_bytes=MAX_OUTPUT_BYTES
                )
            except TimeoutError:
                # the SDK's message repeats the command, which may hold a secret: say it without the command
                return _error(
                    "command_timeout",
                    f"The command did not finish within {timeout_seconds}s on pod {target.id}.",
                    "Raise timeout_seconds, or start long jobs in the background (nohup ... > log 2>&1 &) and read the log.",
                )
            except OutputLimitExceeded:
                return _error(
                    "output_too_large",
                    f"The command printed more than {MAX_OUTPUT_BYTES} bytes on pod {target.id}, so its output was cut off"
                    " and the session closed. The command may still be running on the pod.",
                    "Before running it again, check whether it is still running (e.g. ps aux) and stop it if needed."
                    " Then redirect its output to a file on the pod (cmd > out.log 2>&1) and read part of it,"
                    " e.g. tail -n 200 out.log.",
                )
            except LiumError as e:
                return _error(e.code or type(e).__name__, _redact(str(e), command), _redact(e.hint, command))
            except Exception as e:  # noqa: BLE001
                return _error(type(e).__name__, _redact(str(e), command))
            return {
                "ok": True,
                "exit_code": result.get("exit_code"),
                "stdout": _tail(result.get("stdout")),
                "stderr": _tail(result.get("stderr")),
            }

        return _guarded(call)

    def remove_pod(self, pod: str) -> Dict[str, Any]:
        def call():
            target = self._find_pod(pod)
            self.client.rm(target)
            with self._cleanup:
                unresolved = list(self._unresolved)
            for rental in unresolved:  # a confirmed removal settles an unresolved rental's pod
                with rental.lock:
                    if target.id in rental.pod_ids:
                        rental.pod_ids.discard(target.id)
                        rental.removed.add(target.id)
            self._reconcile()
            return {"ok": True, "removed": target.id, "name": target.name}

        return _guarded(call)

    def get_balance(self) -> Dict[str, Any]:
        return _guarded(lambda: {"ok": True, "balance_usd": self.client.balance()})


def tool_functions(tools: LiumTools) -> List[Callable[..., Awaitable[Dict[str, Any]]]]:
    """The functions the server registers; their names and docstrings are what the model sees.

    They are async and run each blocking SDK call in a worker thread: mcp 1.x calls a sync tool inside its event loop,
    so a slow rental would otherwise stall every other request, including the remove_pod that stops billing."""

    async def list_gpus(gpu_type: Optional[str] = None, gpu_count: Optional[int] = None, limit: int = 20) -> Dict[str, Any]:
        """Find GPU nodes for rent on Lium, cheapest first. Use before rent_pod to pick an executor_id.
        gpu_type filters by model ("H100", "RTX 4090", "B200"); gpu_count by GPUs on the node."""
        return await asyncio.to_thread(tools.list_gpus, gpu_type, gpu_count, limit)

    async def rent_pod(
        executor_id: str,
        name: str,
        template_id: Optional[str] = None,
        timeout_seconds: int = 600,
        known_pod_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Rent a GPU pod on a node from list_gpus and wait until it is running. This spends money: the pod is
        billed per second at the node's hourly price until remove_pod. Give it a short unique name. If the result
        has status "pending", the pod is already rented: do not rent again, poll list_pods or remove_pod its pod_id.
        If it has status "recovered", nothing new was rented: it lists pods earlier calls rented that you have not
        used yet. To rent another pod next to those, pass their ids as known_pod_ids. One rent_pod runs at a time:
        while one is still running, another returns the error rent_in_progress and rents nothing."""
        rental = _Rental()
        try:
            result = await asyncio.to_thread(
                tools.rent_pod, executor_id, name, template_id, timeout_seconds, rental, known_pod_ids
            )
        except asyncio.CancelledError:
            # the thread runs on; whatever pod it rents is removed, since the host will never learn its id
            tools.cancel_rental(rental)
            raise
        tools.deliver(rental)
        return result

    async def list_pods() -> Dict[str, Any]:
        """List your pods with their status, GPU, price per hour and SSH command."""
        receiver = _Rental()
        try:
            result = await asyncio.to_thread(tools.list_pods, receiver)
        except asyncio.CancelledError:
            tools.cancel_rental(receiver)  # recovered pods it would have reported wait for the next call
            raise
        tools.deliver(receiver)
        return result

    async def run_command(pod: str, command: str, timeout_seconds: int = 300) -> Dict[str, Any]:
        """Run a shell command on a running pod (by pod id, or a huid or name that matches one pod) and return exit
        code, stdout and stderr."""
        return await asyncio.to_thread(tools.run_command, pod, command, timeout_seconds)

    async def remove_pod(pod: str) -> Dict[str, Any]:
        """Remove a pod (by pod id, or a huid or name that matches one pod). Billing stops; anything on its disk is gone."""
        return await asyncio.to_thread(tools.remove_pod, pod)

    async def get_balance() -> Dict[str, Any]:
        """Your Lium account balance in USD."""
        return await asyncio.to_thread(tools.get_balance)

    return [list_gpus, rent_pod, list_pods, run_command, remove_pod, get_balance]
