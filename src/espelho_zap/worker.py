"""Single-WIP delivery worker with leases and bounded exponential retry."""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .ledger import MirrorLedger, RouteMissingError
from .media import remove_orphaned_spool_files
from .models import InboundEvent, MediaAttachment, opaque_ref
from .routing import PolicyError, VideoPolicy, enforce_video_policy, require_topic_route
from .transport import (
    Transport,
    TransportError,
    delivery_idempotency_key,
    remove_managed_media,
)


@dataclass(frozen=True, slots=True)
class WorkerResult:
    status: str
    attempt_no: int = 0
    error_code: str = ""
    media_removed: int = 0


class _LeaseHeartbeat:
    """Renew delivery and runtime ownership while a transport call blocks."""

    def __init__(self, worker: "MirrorWorker", delivery_id: int):
        self.worker = worker
        self.delivery_id = delivery_id
        self.interval = max(
            0.2,
            min(worker.lease_seconds, worker.runtime_lock_seconds) / 3,
        )
        self.stop_event = threading.Event()
        self.failed = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="espelho-zap-lease-heartbeat",
            daemon=True,
        )

    def _run(self) -> None:
        try:
            with MirrorLedger(self.worker.ledger.db_path) as ledger:
                while not self.stop_event.wait(self.interval):
                    now = int(time.time())
                    runtime_ok = ledger.renew_runtime_lock(
                        self.worker.profile_id,
                        self.worker.worker_id,
                        now=now,
                        lease_seconds=self.worker.runtime_lock_seconds + 1,
                    )
                    delivery_ok = ledger.renew_delivery_lease(
                        self.delivery_id,
                        self.worker.worker_id,
                        now=now,
                        lease_seconds=self.worker.lease_seconds + 1,
                    )
                    if not runtime_ok or not delivery_ok:
                        self.failed.set()
                        return
        except Exception:
            self.failed.set()

    def __enter__(self) -> "_LeaseHeartbeat":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(1.0, self.interval * 2))

    @property
    def healthy(self) -> bool:
        return not self.failed.is_set()


class MirrorWorker:
    def __init__(
        self,
        ledger: MirrorLedger,
        transport: Transport,
        *,
        worker_id: str | None = None,
        profile_id: str = "default",
        runtime_lock_seconds: int = 120,
        lease_seconds: int = 60,
        max_attempts: int = 5,
        base_backoff_seconds: int = 5,
        max_backoff_seconds: int = 3600,
        allowed_temp_root: str | Path | None = None,
        video_policy: VideoPolicy | str = VideoPolicy.BLOCK,
        media_retention_seconds: int = 48 * 60 * 60,
    ):
        resolved_worker_id = worker_id or f"worker-{uuid.uuid4().hex}"
        if not resolved_worker_id.strip() or not profile_id.strip():
            raise ValueError("invalid_worker_id")
        if (
            lease_seconds <= 0
            or runtime_lock_seconds <= 0
            or max_attempts <= 0
            or base_backoff_seconds < 0
            or media_retention_seconds < 0
        ):
            raise ValueError("invalid_worker_limits")
        self.ledger = ledger
        self.transport = transport
        self.worker_id = resolved_worker_id
        self.profile_id = (
            profile_id
            if re.fullmatch(r"profile:[0-9a-f]{64}", profile_id)
            else opaque_ref("profile", profile_id)
        )
        self.runtime_lock_seconds = int(runtime_lock_seconds)
        self.lease_seconds = int(lease_seconds)
        self.max_attempts = int(max_attempts)
        self.base_backoff_seconds = int(base_backoff_seconds)
        self.max_backoff_seconds = int(max_backoff_seconds)
        self.allowed_temp_root = allowed_temp_root
        self.video_policy = VideoPolicy(video_policy)
        self.media_retention_seconds = int(media_retention_seconds)
        self._run_lock = threading.Lock()

    def ingest(self, event: InboundEvent, *, now: float | int | None = None) -> int | None:
        if event.source_profile_id != self.profile_id:
            raise ValueError("source_profile_mismatch")
        _, delivery_id, blocked_reason = self.ledger.capture_event(event, now=now)
        if blocked_reason:
            raise RouteMissingError(blocked_reason)
        return delivery_id

    def run_once(self, *, now: float | int | None = None) -> WorkerResult:
        if not self._run_lock.acquire(blocking=False):
            return WorkerResult("busy")
        try:
            timestamp = int(time.time() if now is None else now)
            if not self._ensure_runtime_lock(timestamp):
                return WorkerResult("standby")
            cleaned = self._cleanup_media(timestamp)
            remove_orphaned_spool_files(
                self.allowed_temp_root,
                self.ledger.managed_media_paths(),
                now=timestamp,
            )
            claim = self.ledger.claim_next(
                self.worker_id,
                source_profile_id=self.profile_id,
                now=timestamp,
                lease_seconds=self.lease_seconds,
            )
            if claim is None:
                return WorkerResult("idle", media_removed=cleaned)
            try:
                require_topic_route(claim.route)
                enforce_video_policy(claim.event, self.video_policy)
                with _LeaseHeartbeat(self, claim.delivery_id) as heartbeat:
                    result = self.transport.send(
                        claim.event,
                        claim.route,
                        idempotency_key=delivery_idempotency_key(claim.event, claim.route),
                    )
                if not heartbeat.healthy:
                    raise TransportError(
                        "lease_renewal_failed",
                        retryable=False,
                        outcome_unknown=True,
                    )
            except PolicyError as exc:
                self.ledger.mark_failed(
                    claim,
                    error_code=exc.code,
                    retry_at=None,
                    permanent=True,
                    max_attempts=self.max_attempts,
                    now=timestamp,
                )
                return WorkerResult("dead", claim.attempt_no, exc.code)
            except TransportError as exc:
                if exc.outcome_unknown:
                    self.ledger.mark_uncertain(claim, error_code=exc.code, now=timestamp)
                    return WorkerResult("uncertain", claim.attempt_no, exc.code)
                return self._retry_or_dead(claim, timestamp, exc.code, not exc.retryable)
            except Exception:
                # Never include exception text: URLs, paths, contacts, or tokens
                # can appear in third-party exception messages.
                self.ledger.mark_uncertain(
                    claim, error_code="delivery_outcome_unknown", now=timestamp
                )
                return WorkerResult(
                    "uncertain", claim.attempt_no, "delivery_outcome_unknown"
                )

            self.ledger.mark_sent(
                claim,
                result.remote_ids,
                now=timestamp,
                media_retention_seconds=self.media_retention_seconds,
            )
            removed = cleaned + self._cleanup_media(timestamp)
            return WorkerResult("sent", claim.attempt_no, media_removed=removed)
        finally:
            self._run_lock.release()

    def run_bounded(
        self,
        *,
        max_items: int = 100,
        max_seconds: float = 50.0,
    ) -> tuple[WorkerResult, ...]:
        """Drain sequentially while preserving global WIP=1 and hard bounds."""

        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or max_items <= 0
            or isinstance(max_seconds, bool)
            or not isinstance(max_seconds, (int, float))
            or float(max_seconds) <= 0
        ):
            raise ValueError("invalid_drain_limits")
        deadline = time.monotonic() + float(max_seconds)
        results: list[WorkerResult] = []
        while len(results) < max_items and time.monotonic() < deadline:
            result = self.run_once()
            if result.status in {"idle", "busy", "standby"}:
                break
            results.append(result)
        return tuple(results)

    def _ensure_runtime_lock(self, now: float | int | None) -> bool:
        return self.ledger.acquire_runtime_lock(
            self.profile_id,
            self.worker_id,
            now=now,
            lease_seconds=self.runtime_lock_seconds,
        )

    def _cleanup_media(self, now: int) -> int:
        removed = 0
        for path in self.ledger.list_media_cleanup_due(now=now, limit=100):
            if not Path(path).exists():
                self.ledger.mark_media_removed(path)
                removed += 1
                continue
            media = MediaAttachment(
                media_id="cleanup",
                kind="document",
                path=path,
                managed_temp=True,
            )
            if remove_managed_media(media, self.allowed_temp_root):
                self.ledger.mark_media_removed(path)
                removed += 1
            else:
                self.ledger.mark_media_cleanup_failed(path, now=now)
        return removed

    def close(self) -> bool:
        return self.ledger.release_runtime_lock(self.profile_id, self.worker_id)

    def _retry_or_dead(
        self,
        claim,
        now: int,
        error_code: str,
        permanent: bool,
    ) -> WorkerResult:
        delay = min(
            self.max_backoff_seconds,
            self.base_backoff_seconds * (2 ** max(0, claim.attempt_no - 1)),
        )
        state = self.ledger.mark_failed(
            claim,
            error_code=error_code,
            retry_at=now + delay,
            permanent=permanent,
            max_attempts=self.max_attempts,
            now=now,
        )
        return WorkerResult(state, claim.attempt_no, error_code)
