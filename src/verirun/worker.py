"""The worker: claims queued Runs with leases and drives them to a resting state.

The CLI and the dashboard only enqueue work. This process claims one job at a
time, runs Resolve/Plan/Execute/Verify through the same orchestrator the demo
uses, and records whether the job completed or should be retried. A crash
leaves the lease behind; once it expires, another worker claims the same Run and
resumes from the durable journal, reconciling any interrupted write.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event, Thread

from verirun.config import Settings
from verirun.context.company import load_company_context
from verirun.context.task_pack import load_task_pack
from verirun.engine.orchestrator import resume_run, run_task
from verirun.engine.states import RunState
from verirun.llm.client import LLMClient, build_client
from verirun.runs.models import QueueItem
from verirun.runs.store import DEFAULT_LEASE_SECONDS, RunStore
from verirun.runtime import Backends, live_registry, sync_mail

ClientFactory = Callable[[str], LLMClient]
BackendsFactory = Callable[[Settings], Backends]
Clock = Callable[[], datetime]

RESTING_STATES = frozenset(
    {
        RunState.AWAITING_APPROVAL,
        RunState.NEEDS_HUMAN,
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.LIMIT_REACHED,
    }
)


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class Worker:
    """Claims queued work and runs it, one job at a time."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: RunStore | None = None,
        worker_id: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        client_factory: ClientFactory | None = None,
        backends_factory: BackendsFactory | None = None,
        clock: Clock | None = None,
        sync_mail_before_start: bool = True,
    ) -> None:
        self.settings = settings
        self.store = store or RunStore.for_settings(settings)
        self.worker_id = worker_id or default_worker_id()
        self.lease_seconds = lease_seconds
        self._client_factory = client_factory
        self._backends_factory = backends_factory or Backends.from_settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self.sync_mail_before_start = sync_mail_before_start

    def client_for(self, run_id: str) -> LLMClient:
        if self._client_factory is not None:
            return self._client_factory(run_id)
        return build_client(self.settings, session_id=run_id)

    def run_once(self) -> QueueItem | None:
        """Claim and run one job. Returns None when the queue is empty."""
        item = self.store.claim(
            self.worker_id,
            lease_seconds=self.lease_seconds,
            now=self._clock(),
        )
        if item is None:
            return None
        stop = Event()
        keeper = self._start_lease_keeper(item, stop)
        error: Exception | None = None
        try:
            self._execute(item)
        except Exception as exc:  # noqa: BLE001 - a worker failure is recorded, not raised
            error = exc
        except BaseException:
            # A killed worker leaves its lease behind on purpose. The lease
            # expires, another worker claims the Run, and the journal decides
            # whether the interrupted side effect must be reconciled.
            raise
        finally:
            stop.set()
            keeper.join(timeout=5)
        if error is not None:
            return self.store.fail_queue_item(
                item.id,
                self.worker_id,
                f"{type(error).__name__}: {error}",
                now=self._clock(),
            )
        return self.store.complete_queue_item(
            item.id, self.worker_id, now=self._clock()
        )

    def _start_lease_keeper(self, item: QueueItem, stop: Event) -> Thread:
        interval = max(1.0, self.lease_seconds / 3)

        def keep() -> None:
            while not stop.wait(interval):
                if not self.store.renew_lease(
                    item.id,
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                    now=self._clock(),
                ):
                    return

        thread = Thread(target=keep, name=f"lease-{item.id}", daemon=True)
        thread.start()
        return thread

    def _execute(self, item: QueueItem) -> None:
        run = self.store.get_run(item.run_id)
        context = load_company_context(self.settings.company_dir)
        task_pack = load_task_pack(self.settings.tasks_dir / f"{run.task_id}.yaml")
        client = self.client_for(run.id)
        backends = self._backends_factory(self.settings)
        with live_registry(
            self.settings,
            context,
            task_pack,
            client,
            run.id,
            backends=backends,
            store=self.store,
        ) as registry:
            if item.kind == "start" and run.plan is None:
                if self.sync_mail_before_start:
                    sync_mail(backends)
                run_task(
                    run.request,
                    task_pack,
                    context,
                    client,
                    self.store,
                    registry,
                    accounting=backends.verifier_accounting,
                    shared_root=backends.mail.document_root,
                    evidence_root=self.settings.run_db.parent,
                    max_steps=self.settings.max_steps,
                    max_cost_usd=self.settings.max_cost_usd,
                    prices=self.settings.prices,
                    run_id=run.id,
                )
            else:
                resume_run(
                    run.id,
                    task_pack,
                    client,
                    self.store,
                    registry,
                    accounting=backends.verifier_accounting,
                    shared_root=backends.mail.document_root,
                    evidence_root=self.settings.run_db.parent,
                    max_steps=self.settings.max_steps,
                    max_cost_usd=self.settings.max_cost_usd,
                    prices=self.settings.prices,
                )

    def run_forever(
        self,
        *,
        poll_interval_s: float = 5.0,
        max_items: int | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> int:
        """Poll the queue until stopped; returns the number of jobs processed."""
        processed = 0
        while stop is None or not stop():
            if max_items is not None and processed >= max_items:
                return processed
            item = self.run_once()
            if item is None:
                if max_items is not None:
                    return processed
                time.sleep(poll_interval_s)
                continue
            processed += 1
        return processed


def enqueue_start(
    settings: Settings,
    request: str,
    task_id: str,
    *,
    store: RunStore | None = None,
    run_id: str | None = None,
) -> tuple[RunStore, str]:
    """Create a Run in ``created`` and queue its start job."""
    store = store or RunStore.for_settings(settings)
    run = store.create_queued_run(request, task_id, run_id=run_id)
    return store, run.id


def enqueue_resume(store: RunStore, run_id: str) -> QueueItem:
    """Queue a parked Run for a worker, after an approval decision or a fix."""
    return store.enqueue(run_id, "resume")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verirun worker",
        description="Claim queued Runs and execute them with leases.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one queued job and exit",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="stop after this many jobs (default: run forever)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="seconds to wait when the queue is empty (default 5)",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=DEFAULT_LEASE_SECONDS,
        help=f"how long a claimed job stays leased (default {DEFAULT_LEASE_SECONDS})",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="name recorded on the lease (default: hostname-pid)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    store: RunStore | None = None,
    client_factory: ClientFactory | None = None,
    clock: Clock | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or Settings()
    worker = Worker(
        settings,
        store=store,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        client_factory=client_factory,
        clock=clock,
    )
    if args.once:
        item = worker.run_once()
        if item is None:
            print("No queued work.")
            return 0
        print(f"Ran {item.run_id} ({item.kind}); queue item {item.status}.")
        return 0
    try:
        processed = worker.run_forever(
            poll_interval_s=args.poll_interval,
            max_items=args.max_items,
        )
    except KeyboardInterrupt:
        print("Worker stopped.", file=sys.stderr)
        return 0
    print(f"Worker stopped after {processed} job(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
