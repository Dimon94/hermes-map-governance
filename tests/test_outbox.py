from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread
from time import sleep

import pytest

from map_governance.outbox import (
    EffectConfirmation,
    EffectRetryableError,
    EffectTerminalError,
    OutboxConflictError,
    OutboxDispatcher,
    OutboxLeaseError,
    OutboxRepository,
    OutboxRuntime,
    OutboxSettings,
)
from map_governance.storage import PluginStorage


def _claim_from_independent_process(storage_root, owner_id, start, results):
    repository = OutboxRepository(Path(storage_root))
    start.wait(timeout=5)
    claim = repository.claim_next(
        owner_id=owner_id,
        now="2026-08-23T10:00:00Z",
        lease_seconds=30,
    )
    results.put(claim.owner_id if claim is not None else None)


def test_committed_intent_survives_restart_and_rejects_payload_reuse(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    PluginStorage(storage_root).check_readiness()
    first_repository = OutboxRepository(storage_root)

    first = first_repository.enqueue(
        effect_id="decision:I_atlas_41:market-001",
        effect_type="tracker.decision",
        map_id="I_atlas_41",
        payload={"decision_id": "market-001", "rationale": "Start narrow."},
        created_at="2026-08-23T10:00:00Z",
    )

    restarted = OutboxRepository(storage_root)
    replay = restarted.enqueue(
        effect_id="decision:I_atlas_41:market-001",
        effect_type="tracker.decision",
        map_id="I_atlas_41",
        payload={"decision_id": "market-001", "rationale": "Start narrow."},
        created_at="2026-08-23T10:05:00Z",
    )

    assert first.created is True
    assert replay.created is False
    assert replay.intent == first.intent
    assert restarted.intent(first.intent.effect_id) == first.intent
    assert first.intent.state == "pending"
    assert first.intent.payload_hash.startswith("sha256:")
    assert Path(restarted.database).is_file()

    with pytest.raises(OutboxConflictError, match="different payload"):
        restarted.enqueue(
            effect_id=first.intent.effect_id,
            effect_type="tracker.decision",
            map_id="I_atlas_41",
            payload={"decision_id": "market-001", "rationale": "Launch wide."},
            created_at="2026-08-23T10:06:00Z",
        )


def test_repeatable_occurrence_base_rejects_prior_payload_mismatch(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = OutboxRepository(storage_root)
    base = "stage-transition:repeatable-pair"
    repository.enqueue(
        effect_id=f"{base}:1",
        effect_type="tracker.stage-transition",
        map_id="I_atlas_41",
        payload={"expected_stage": "delivery", "requested_stage": "decision"},
        created_at="2026-08-23T10:00:00Z",
    )

    with pytest.raises(OutboxConflictError, match="occurrence identity"):
        repository.enqueue_occurrence(
            effect_id_base=base,
            effect_type="tracker.stage-transition",
            map_id="I_atlas_41",
            payload={"expected_stage": "delivery", "requested_stage": "acceptance"},
            created_at="2026-08-23T10:01:00Z",
        )


def test_expired_lease_is_reclaimed_and_fences_the_old_owner(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    PluginStorage(storage_root).check_readiness()
    repository_a = OutboxRepository(storage_root)
    repository_b = OutboxRepository(storage_root)
    repository_a.enqueue(
        effect_id="transition:I_atlas_41:authorized-delivery",
        effect_type="tracker.stage-transition",
        map_id="I_atlas_41",
        payload={"expected_stage": "authorized", "requested_stage": "delivery"},
        created_at="2026-08-23T10:00:00Z",
    )

    first = repository_a.claim_next(
        owner_id="process-a",
        now="2026-08-23T10:00:00Z",
        lease_seconds=30,
    )

    assert first is not None
    assert first.owner_id == "process-a"
    assert first.attempt_number == 1
    assert first.lease_expires_at == "2026-08-23T10:00:30.000000Z"
    assert (
        repository_b.claim_next(
            owner_id="process-b",
            now="2026-08-23T10:00:20Z",
            lease_seconds=30,
        )
        is None
    )

    reclaimed = repository_b.claim_next(
        owner_id="process-b",
        now="2026-08-23T10:00:31Z",
        lease_seconds=30,
    )

    assert reclaimed is not None
    assert reclaimed.owner_id == "process-b"
    assert reclaimed.attempt_number == 2
    with pytest.raises(OutboxLeaseError, match="does not own"):
        repository_a.acknowledge(
            effect_id=first.effect_id,
            owner_id="process-a",
            now="2026-08-23T10:00:32Z",
            acknowledgment={"stage": "delivery"},
        )
    succeeded = repository_b.acknowledge(
        effect_id=reclaimed.effect_id,
        owner_id="process-b",
        now="2026-08-23T10:00:32Z",
        acknowledgment={"stage": "delivery"},
    )

    assert succeeded.state == "succeeded"
    assert succeeded.acknowledgment == {"stage": "delivery"}
    assert [
        attempt.outcome for attempt in repository_a.attempt_history(first.effect_id)
    ] == [
        "lease_expired",
        "succeeded",
    ]


def test_fractional_timestamp_does_not_expire_lease_early(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:fractional-lease"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at="2026-08-23T10:00:00.900000Z",
    )
    claim = repository.claim_next(
        owner_id="process-a",
        now="2026-08-23T10:00:00.900000Z",
        lease_seconds=1,
    )

    assert claim is not None
    assert (
        repository.claim_next(
            owner_id="process-b",
            now="2026-08-23T10:00:01Z",
            lease_seconds=1,
        )
        is None
    )
    assert (
        repository.acknowledge(
            effect_id=effect_id,
            owner_id="process-a",
            now="2026-08-23T10:00:01.500000Z",
            acknowledgment={"marker": effect_id},
        ).state
        == "succeeded"
    )


def test_owner_can_renew_and_release_without_losing_attempt_history(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    PluginStorage(storage_root).check_readiness()
    repository = OutboxRepository(storage_root)
    effect_id = "session:I_atlas_41:wake-001"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="session.resume",
        map_id="I_atlas_41",
        payload={"session_id": "mapgov-root", "message": "Review the decision."},
        created_at="2026-08-23T10:00:00Z",
    )
    repository.claim_next(
        owner_id="process-a",
        now="2026-08-23T10:00:00Z",
        lease_seconds=30,
    )

    renewed = repository.renew(
        effect_id=effect_id,
        owner_id="process-a",
        now="2026-08-23T10:00:20Z",
        lease_seconds=30,
    )
    released = repository.release(
        effect_id=effect_id,
        owner_id="process-a",
        now="2026-08-23T10:00:21Z",
    )
    reclaimed = repository.claim_next(
        owner_id="process-b",
        now="2026-08-23T10:00:21Z",
        lease_seconds=30,
    )

    assert renewed.lease_expires_at == "2026-08-23T10:00:50.000000Z"
    assert released.state == "pending"
    assert reclaimed is not None
    assert reclaimed.attempt_number == 2
    assert [attempt.outcome for attempt in repository.attempt_history(effect_id)] == [
        "released",
        "claimed",
    ]


def test_two_independent_processes_cannot_claim_the_same_action(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    PluginStorage(storage_root).check_readiness()
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:process-race"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at="2026-08-23T10:00:00Z",
    )
    context = get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_from_independent_process,
            args=(str(storage_root), owner_id, start, results),
        )
        for owner_id in ("process-a", "process-b")
    ]
    for process in processes:
        process.start()
    start.set()
    owners = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(owner for owner in owners if owner is not None) in (
        ["process-a"],
        ["process-b"],
    )
    assert owners.count(None) == 1
    assert repository.intent(effect_id).attempt_count == 1


def test_recovery_skips_effect_types_without_an_adapter(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = OutboxRepository(storage_root)
    effect_id = "session-resume:I_atlas_41:pending"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="session.resume",
        map_id="I_atlas_41",
        payload={"root_session_id": "root-41"},
        created_at="2026-08-23T10:00:00Z",
    )
    dispatcher = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": MarkerEffectAdapter()},
        clock=lambda: "2026-08-23T10:00:01Z",
    )

    assert dispatcher.dispatch_next(owner_id="profileless-runtime") is None
    assert repository.intent(effect_id).state == "pending"


def test_background_grace_leaves_new_intent_for_synchronous_facade(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:foreground-first"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at="2026-08-23T10:00:00Z",
    )

    assert (
        repository.claim_next(
            owner_id="background-runtime",
            now="2026-08-23T10:00:00.500000Z",
            lease_seconds=30,
            pending_before="2026-08-23T09:59:59.500000Z",
        )
        is None
    )
    assert (
        repository.claim(
            effect_id=effect_id,
            owner_id="synchronous-facade",
            now="2026-08-23T10:00:00.500000Z",
            lease_seconds=30,
        )
        is not None
    )


class MarkerEffectAdapter:
    def __init__(self) -> None:
        self.markers: dict[str, dict] = {}
        self.calls: list[str] = []

    def readback(self, intent):
        marker = self.markers.get(intent.effect_id)
        return EffectConfirmation(marker) if marker is not None else None

    def apply(self, intent):
        self.calls.append(intent.effect_id)
        self.markers[intent.effect_id] = {"external_marker": intent.effect_id}


class SimulatedProcessCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "crash_point",
    (
        "after_claim",
        "after_initial_readback",
        "before_external_call",
        "after_external_call",
        "after_confirmation",
        "after_completion",
        "before_acknowledgment",
        "after_acknowledgment",
    ),
)
def test_restart_converges_across_every_dispatch_state_boundary(
    tmp_path,
    crash_point,
):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = OutboxRepository(storage_root)
    effect_id = f"test-boundary:{crash_point}"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at="2026-08-23T10:00:00Z",
    )
    backend = MarkerEffectAdapter()
    completed = set()

    def complete(intent, _confirmation):
        completed.add(intent.effect_id)

    def crash(point, _intent):
        if point == crash_point:
            raise SimulatedProcessCrash()

    dispatcher = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": backend},
        clock=lambda: "2026-08-23T10:00:00Z",
        settings=OutboxSettings(lease_seconds=30),
        completion=complete,
        crash_injector=crash,
    )

    with pytest.raises(SimulatedProcessCrash):
        dispatcher.dispatch_next(owner_id="process-before-crash")

    restarted = OutboxDispatcher(
        repository=OutboxRepository(storage_root),
        adapters={"test.marker": backend},
        clock=lambda: "2026-08-23T10:00:31Z",
        settings=OutboxSettings(lease_seconds=30),
        completion=complete,
    )
    outcome = restarted.dispatch_next(owner_id="process-after-restart")
    status = repository.intent(effect_id)

    assert status is not None and status.state == "succeeded"
    assert backend.calls == [effect_id]
    assert completed == {effect_id}
    if crash_point == "after_acknowledgment":
        assert outcome is None
        assert status.attempt_count == 1
    else:
        assert outcome is not None and outcome.state == "succeeded"
        assert status.attempt_count == 2


def test_restart_reads_back_success_after_call_before_ack_without_duplicate(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    PluginStorage(storage_root).check_readiness()
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:decision-001"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at="2026-08-23T10:00:00Z",
    )
    backend = MarkerEffectAdapter()

    def crash_after_external_call(point, _intent):
        if point == "after_external_call":
            raise SimulatedProcessCrash()

    crashed = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": backend},
        clock=lambda: "2026-08-23T10:00:00Z",
        settings=OutboxSettings(lease_seconds=30),
        crash_injector=crash_after_external_call,
    )

    with pytest.raises(SimulatedProcessCrash):
        crashed.dispatch_next(owner_id="process-before-crash")

    assert backend.calls == [effect_id]
    assert repository.intent(effect_id).state == "leased"

    restarted = OutboxDispatcher(
        repository=OutboxRepository(storage_root),
        adapters={"test.marker": backend},
        clock=lambda: "2026-08-23T10:00:31Z",
        settings=OutboxSettings(lease_seconds=30),
    )
    outcome = restarted.dispatch_next(owner_id="process-after-restart")

    assert outcome is not None
    assert outcome.state == "succeeded"
    assert outcome.reconciled_by_readback is True
    assert backend.calls == [effect_id]
    assert repository.intent(effect_id).acknowledgment == {"external_marker": effect_id}


class RecoveringMarkerAdapter(MarkerEffectAdapter):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def apply(self, intent):
        self.calls.append(intent.effect_id)
        if len(self.calls) <= self.failures:
            raise EffectRetryableError("temporary downstream outage")
        self.markers[intent.effect_id] = {"external_marker": intent.effect_id}


class BlockingMarkerAdapter(MarkerEffectAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def apply(self, intent):
        self.calls.append(intent.effect_id)
        self.started.set()
        assert self.release.wait(timeout=5)
        self.markers[intent.effect_id] = {"external_marker": intent.effect_id}


def test_dispatcher_renews_lease_while_external_call_is_running(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:slow-call"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    backend = BlockingMarkerAdapter()
    dispatcher = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": backend},
        clock=lambda: datetime.now(timezone.utc),
        settings=OutboxSettings(lease_seconds=1),
    )
    outcome = []
    worker = Thread(
        target=lambda: outcome.append(dispatcher.dispatch_next(owner_id="process-a"))
    )
    worker.start()
    assert backend.started.wait(timeout=2)

    sleep(1.2)
    assert (
        OutboxRepository(storage_root).claim_next(
            owner_id="process-b",
            now=datetime.now(timezone.utc).isoformat(),
            lease_seconds=1,
        )
        is None
    )
    backend.release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert outcome[0].state == "succeeded"
    assert backend.calls == [effect_id]


def test_retryable_failure_backs_off_automatically_and_preserves_history(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    PluginStorage(storage_root).check_readiness()
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:retry-001"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at="2026-08-23T10:00:00Z",
    )
    now = ["2026-08-23T10:00:00Z"]
    backend = RecoveringMarkerAdapter(failures=2)
    dispatcher = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": backend},
        clock=lambda: now[0],
        settings=OutboxSettings(
            lease_seconds=30,
            base_retry_seconds=10,
            max_retry_seconds=40,
            max_attempts=4,
        ),
    )

    first = dispatcher.dispatch_next(owner_id="process-a")
    now[0] = "2026-08-23T10:00:09Z"
    not_due = dispatcher.dispatch_next(owner_id="process-a")
    now[0] = "2026-08-23T10:00:10Z"
    second = dispatcher.dispatch_next(owner_id="process-a")
    now[0] = "2026-08-23T10:00:30Z"
    succeeded = dispatcher.dispatch_next(owner_id="process-a")

    assert first is not None and first.state == "retry_scheduled"
    assert first.next_attempt_at == "2026-08-23T10:00:10.000000Z"
    assert not_due is None
    assert (
        second is not None and second.next_attempt_at == "2026-08-23T10:00:30.000000Z"
    )
    assert succeeded is not None and succeeded.state == "succeeded"
    assert backend.calls == [effect_id, effect_id, effect_id]
    assert [attempt.outcome for attempt in repository.attempt_history(effect_id)] == [
        "retry_scheduled",
        "retry_scheduled",
        "succeeded",
    ]
    assert [
        attempt.error_type for attempt in repository.attempt_history(effect_id)
    ] == [
        "EffectRetryableError",
        "EffectRetryableError",
        None,
    ]


def test_runtime_revisits_future_retry_schedule_without_another_request(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:automatic-retry"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    backend = RecoveringMarkerAdapter(failures=1)
    dispatcher = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": backend},
        clock=lambda: datetime.now(timezone.utc),
        settings=OutboxSettings(base_retry_seconds=1),
    )
    runtime = OutboxRuntime(
        dispatcher=dispatcher,
        owner_id="resident-runtime",
        poll_seconds=0.05,
    )

    runtime.start()
    deadline = datetime.now(timezone.utc).timestamp() + 4
    while (
        repository.intent(effect_id).state != "succeeded"
        and datetime.now(timezone.utc).timestamp() < deadline
    ):
        sleep(0.05)
    runtime.stop()

    assert repository.intent(effect_id).state == "succeeded"
    assert backend.calls == [effect_id, effect_id]
    assert [attempt.outcome for attempt in repository.attempt_history(effect_id)] == [
        "retry_scheduled",
        "succeeded",
    ]


def test_runtime_reclaims_a_restart_lease_after_its_future_expiry(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:restart-before-expiry"
    now = datetime.now(timezone.utc)
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at=now.isoformat(),
    )
    repository.claim_next(
        owner_id="crashed-process",
        now=now.isoformat(),
        lease_seconds=1,
    )
    backend = MarkerEffectAdapter()
    runtime = OutboxRuntime(
        dispatcher=OutboxDispatcher(
            repository=repository,
            adapters={"test.marker": backend},
            clock=lambda: datetime.now(timezone.utc),
            settings=OutboxSettings(lease_seconds=1),
        ),
        owner_id="restarted-runtime",
        poll_seconds=0.05,
    )

    runtime.start()
    deadline = datetime.now(timezone.utc).timestamp() + 4
    while (
        repository.intent(effect_id).state != "succeeded"
        and datetime.now(timezone.utc).timestamp() < deadline
    ):
        sleep(0.05)
    runtime.stop()

    assert repository.intent(effect_id).state == "succeeded"
    assert backend.calls == [effect_id]
    assert [attempt.outcome for attempt in repository.attempt_history(effect_id)] == [
        "lease_expired",
        "succeeded",
    ]


class TerminalMarkerAdapter(MarkerEffectAdapter):
    def apply(self, intent):
        self.calls.append(intent.effect_id)
        raise EffectTerminalError("downstream rejected the governance payload")


def test_terminal_failure_stops_and_explicit_repair_is_auditable(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    PluginStorage(storage_root).check_readiness()
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:terminal-001"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at="2026-08-23T10:00:00Z",
    )
    backend = TerminalMarkerAdapter()
    dispatcher = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": backend},
        clock=lambda: "2026-08-23T10:00:01Z",
    )

    terminal = dispatcher.dispatch_next(owner_id="process-a")
    repeated = dispatcher.dispatch_next(owner_id="process-a")
    status = repository.operator_status(effect_id)

    assert terminal is not None and terminal.state == "terminal"
    assert terminal.terminal_reason == (
        "terminal_failure: downstream rejected the governance payload"
    )
    assert repeated is None
    assert backend.calls == [effect_id]
    assert status["repair_action"] == {
        "action": "repair_outbox",
        "effect_id": effect_id,
        "requires": ["repair_id", "note"],
    }

    repaired = repository.repair(
        effect_id=effect_id,
        repair_id="repair-terminal-001",
        note="Payload policy was corrected downstream; retry the same marker.",
        requested_at="2026-08-23T10:05:00Z",
    )
    replay = repository.repair(
        effect_id=effect_id,
        repair_id="repair-terminal-001",
        note="Payload policy was corrected downstream; retry the same marker.",
        requested_at="2026-08-23T10:06:00Z",
    )

    assert repaired.created is True
    assert replay.created is False
    assert repaired.intent.state == "pending"
    assert repaired.intent.attempt_count == 1
    assert repository.operator_status(effect_id)["repairs"] == [
        {
            "repair_id": "repair-terminal-001",
            "note": "Payload policy was corrected downstream; retry the same marker.",
            "requested_at": "2026-08-23T10:05:00.000000Z",
            "prior_terminal_reason": (
                "terminal_failure: downstream rejected the governance payload"
            ),
        }
    ]

    with pytest.raises(OutboxConflictError, match="repair identity"):
        repository.repair(
            effect_id=effect_id,
            repair_id="repair-terminal-001",
            note="A different repair story.",
            requested_at="2026-08-23T10:07:00Z",
        )


def test_repair_starts_a_fresh_retry_budget_without_deleting_attempt_history(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = OutboxRepository(storage_root)
    effect_id = "tracker:I_atlas_41:retry-budget-repair"
    repository.enqueue(
        effect_id=effect_id,
        effect_type="test.marker",
        map_id="I_atlas_41",
        payload={"marker": effect_id},
        created_at="2026-08-23T10:00:00Z",
    )
    backend = RecoveringMarkerAdapter(failures=2)
    first_cycle = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": backend},
        clock=lambda: "2026-08-23T10:00:00Z",
        settings=OutboxSettings(max_attempts=1),
    )
    assert first_cycle.dispatch_next(owner_id="process-a").state == "terminal"
    repository.repair(
        effect_id=effect_id,
        repair_id="repair-retry-budget-001",
        note="The downstream outage was investigated; begin a fresh retry cycle.",
        requested_at="2026-08-23T10:01:00Z",
    )
    repaired_cycle = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": backend},
        clock=lambda: "2026-08-23T10:01:00Z",
        settings=OutboxSettings(base_retry_seconds=5, max_attempts=2),
    )

    retry = repaired_cycle.dispatch_next(owner_id="process-b")
    assert retry is not None and retry.state == "retry_scheduled"
    repaired_cycle = OutboxDispatcher(
        repository=repository,
        adapters={"test.marker": backend},
        clock=lambda: "2026-08-23T10:01:05Z",
        settings=OutboxSettings(base_retry_seconds=5, max_attempts=2),
    )
    succeeded = repaired_cycle.dispatch_next(owner_id="process-b")

    assert succeeded is not None and succeeded.state == "succeeded"
    assert [attempt.outcome for attempt in repository.attempt_history(effect_id)] == [
        "terminal",
        "retry_scheduled",
        "succeeded",
    ]
