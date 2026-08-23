from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Lock

import pytest

from map_governance import (
    CEOSessionAmbiguityError,
    CEOSessionRepairRequired,
    MapGovernanceApplication,
)
from map_governance.sessions import (
    BackendSession,
    HermesSessionAdapter,
    canonical_session_identity,
    canonical_session_title,
)
from map_governance.tracker import TrackerIssue, TrackerProject


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAP_ID = "I_atlas_41"
PROFILE = "ceo"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"


class SessionTracker:
    def __init__(self) -> None:
        self.project = TrackerProject(
            id="PVT_acme_7",
            owner="acme",
            owner_type="organization",
            number=7,
            title="Acme CEO portfolio",
            url=PROJECT_URL,
        )
        self.issue = TrackerIssue(
            id=MAP_ID,
            repository="acme/atlas",
            number=41,
            title="Map the Atlas launch",
            url=ISSUE_URL,
            state="open",
            state_reason=None,
            labels=("map", "map-stage/authorized"),
        )

    def get_project(self, url: str) -> TrackerProject:
        assert url == PROJECT_URL
        return self.project

    def get_issue(self, url: str) -> TrackerIssue:
        assert url == ISSUE_URL
        return self.issue

    def list_decisions(self, url: str):
        assert url == ISSUE_URL
        return []

    def transition_issue_stage(self, *args, **kwargs):
        raise AssertionError("session tests do not transition tracker state")


class ControllableSessionBackend:
    """Controllable boundary below the production HermesSessionAdapter."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.records: dict[str, BackendSession] = {}
        self.messages: dict[str, list[str]] = {}
        self.create_calls: list[dict[str, str]] = []
        self.bootstrap_calls: list[dict[str, str]] = []
        self.skill_calls: list[dict[str, str]] = []
        self.message_ids: dict[str, set[str]] = {}
        self.exact_override: list[str] | None = None
        self.session_configs: dict[str, tuple[str, str]] = {}

    def exact_title_sessions(self, title: str) -> list[BackendSession]:
        with self._lock:
            if self.exact_override is not None:
                return [self.records[session_id] for session_id in self.exact_override]
            return [record for record in self.records.values() if record.title == title]

    def get_session(self, session_id: str) -> BackendSession | None:
        with self._lock:
            return self.records.get(session_id)

    def resolve_resume_session_id(self, session_id: str) -> str:
        with self._lock:
            current = session_id
            for _ in range(32):
                parent = self.records.get(current)
                if parent is None or parent.end_reason != "compression":
                    return current
                children = [
                    row
                    for row in self.records.values()
                    if row.parent_session_id == current
                ]
                if not children:
                    return current
                current = sorted(children, key=lambda row: row.started_at)[-1].id
            return current

    def create_session(
        self,
        session_id: str,
        *,
        profile_name: str,
        source: str,
    ) -> None:
        with self._lock:
            self.create_calls.append(
                {
                    "session_id": session_id,
                    "profile_name": profile_name,
                    "source": source,
                }
            )
            self.records.setdefault(
                session_id,
                BackendSession(
                    id=session_id,
                    title=None,
                    parent_session_id=None,
                    end_reason=None,
                    message_count=0,
                    started_at="2026-08-23T08:00:00Z",
                    last_activity_at="2026-08-23T08:00:00Z",
                ),
            )
            self.messages.setdefault(session_id, [])
            self.message_ids.setdefault(session_id, set())
            self.session_configs.setdefault(
                session_id,
                ("stable CEO system prompt", "map-governance-ceo-tools-v1"),
            )

    def set_session_title(self, session_id: str, title: str) -> None:
        with self._lock:
            collision = next(
                (
                    row
                    for row in self.records.values()
                    if row.title == title and row.id != session_id
                ),
                None,
            )
            if collision is not None:
                raise ValueError(f"title is already used by {collision.id}")
            self.records[session_id] = replace(self.records[session_id], title=title)

    def append_bootstrap_if_empty(
        self,
        session_id: str,
        *,
        content: str,
        idempotency_key: str,
    ) -> bool:
        with self._lock:
            record = self.records[session_id]
            if record.message_count:
                return False
            self.messages[session_id].append(content)
            self.message_ids.setdefault(session_id, set()).add(idempotency_key)
            self.bootstrap_calls.append(
                {
                    "session_id": session_id,
                    "content": content,
                    "idempotency_key": idempotency_key,
                }
            )
            self.records[session_id] = replace(
                record,
                message_count=1,
                last_activity_at="2026-08-23T08:01:00Z",
            )
            return True

    def append_message_once(
        self,
        session_id: str,
        *,
        content: str,
        idempotency_key: str,
    ) -> bool:
        with self._lock:
            message_ids = self.message_ids.setdefault(session_id, set())
            if idempotency_key in message_ids:
                return False
            message_ids.add(idempotency_key)
            self.messages[session_id].append(content)
            self.skill_calls.append(
                {
                    "session_id": session_id,
                    "content": content,
                    "idempotency_key": idempotency_key,
                }
            )
            record = self.records[session_id]
            self.records[session_id] = replace(
                record,
                message_count=record.message_count + 1,
                last_activity_at=max(
                    str(record.last_activity_at or ""),
                    "2026-08-23T08:01:00Z",
                ),
            )
            return True

    def add_existing(
        self,
        session_id: str,
        *,
        title: str,
        messages: tuple[str, ...] = ("prior decision",),
        parent_session_id: str | None = None,
        end_reason: str | None = None,
        started_at: str = "2026-08-20T12:00:00Z",
        last_activity_at: str = "2026-08-22T14:30:00Z",
        system_prompt: str = "stable CEO system prompt",
        toolset_fingerprint: str = "map-governance-ceo-tools-v1",
    ) -> None:
        with self._lock:
            self.records[session_id] = BackendSession(
                id=session_id,
                title=title,
                parent_session_id=parent_session_id,
                end_reason=end_reason,
                message_count=len(messages),
                started_at=started_at,
                last_activity_at=last_activity_at,
            )
            self.messages[session_id] = list(messages)
            self.message_ids[session_id] = set()
            self.session_configs[session_id] = (
                system_prompt,
                toolset_fingerprint,
            )


def _application(
    tmp_path: Path,
    backend: ControllableSessionBackend,
    *,
    storage_root: Path | None = None,
    tracker: SessionTracker | None = None,
) -> MapGovernanceApplication:
    return MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root or tmp_path / "plugin-data" / "map-governance",
        tracker=tracker or SessionTracker(),
        session_runner=HermesSessionAdapter(backend),
        profile_name=PROFILE,
        clock=lambda: datetime(2026, 8, 23, 8, 2, tzinfo=timezone.utc),
    )


def _bind(application: MapGovernanceApplication) -> dict:
    project = application.configure_project(project_url=PROJECT_URL)
    return application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)


def test_unique_exact_session_is_adopted_before_mint(tmp_path):
    backend = ControllableSessionBackend()
    title = canonical_session_title(MAP_ID)
    backend.add_existing("existing-root", title=title)
    application = _application(tmp_path, backend)
    card = _bind(application)

    opened = application.open_map(map_id=card["id"])

    assert opened["ceo_session"] == {
        "state": "ready",
        "root_session_id": "existing-root",
        "live_session_id": "existing-root",
        "last_activity_at": "2026-08-23T08:01:00Z",
    }
    assert backend.create_calls == []
    assert backend.bootstrap_calls == []
    assert len(backend.skill_calls) == 1
    assert "Loaded Skill: map-governance:ceo" in backend.skill_calls[0]["content"]
    assert (
        "Do not create, control, or inspect worker lanes"
        in backend.skill_calls[0]["content"]
    )
    projected = application.board()["maps"][0]["ceo_session"]
    assert projected == {
        "state": "ready",
        "last_activity_at": "2026-08-23T08:01:00Z",
    }


def test_multiple_exact_candidates_require_repair_without_guessing_or_leaking(tmp_path):
    backend = ControllableSessionBackend()
    title = canonical_session_title(MAP_ID)
    backend.add_existing("candidate-a", title=title)
    # A controllable backend can represent legacy/corrupt stores even though
    # current Hermes enforces a unique exact-title index.
    backend.add_existing("candidate-b", title=title)
    backend.exact_override = ["candidate-a", "candidate-b"]
    application = _application(tmp_path, backend)
    card = _bind(application)

    with pytest.raises(CEOSessionAmbiguityError) as raised:
        application.open_map(map_id=card["id"])

    assert raised.value.as_dict() == {
        "type": "repair_required",
        "reason": "multiple_exact_canonical_sessions",
        "candidate_count": 2,
        "retryable": False,
    }
    assert "candidate-a" not in str(raised.value.as_dict())
    assert "candidate-b" not in str(raised.value.as_dict())
    assert backend.create_calls == []
    session = application.map_detail(map_id=card["id"])["ceo_session"]
    assert session == {
        "state": "repair_required",
        "repair": {
            "reason": "multiple_exact_canonical_sessions",
            "candidate_count": 2,
        },
    }


def test_zero_candidates_mints_once_and_persists_one_bootstrap_user_turn(tmp_path):
    backend = ControllableSessionBackend()
    application = _application(tmp_path, backend)
    card = _bind(application)

    first = application.open_map(map_id=card["id"])
    second = application.open_map(map_id=card["id"])

    assert first == second
    assert len(backend.create_calls) == 1
    assert backend.create_calls[0]["profile_name"] == PROFILE
    assert backend.create_calls[0]["source"] == "desktop"
    assert len(backend.bootstrap_calls) == 1
    bootstrap = backend.bootstrap_calls[0]
    assert (
        canonical_session_identity(profile_name=PROFILE, map_id=MAP_ID)
        in bootstrap["content"]
    )
    assert "map-governance:ceo" in bootstrap["content"]
    assert len(backend.skill_calls) == 1
    skill = backend.skill_calls[0]
    assert "Loaded Skill: map-governance:ceo" in skill["content"]
    assert "Do not create, control, or inspect worker lanes" in skill["content"]
    assert "Do not edit implementation worktrees or implement code" in skill["content"]
    assert ISSUE_URL in bootstrap["content"]
    assert "system prompt" not in bootstrap["content"].lower()


def test_repeated_concurrent_opens_and_reconnects_converge_on_one_session(tmp_path):
    backend = ControllableSessionBackend()
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application = _application(tmp_path, backend, storage_root=storage_root)
    card = _bind(application)
    barrier = Barrier(8)

    def open_from_fresh_renderer() -> str:
        reconnect = _application(tmp_path, backend, storage_root=storage_root)
        barrier.wait()
        return reconnect.open_map(map_id=card["id"])["ceo_session"]["root_session_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        roots = list(pool.map(lambda _: open_from_fresh_renderer(), range(8)))

    assert len(set(roots)) == 1
    assert len(backend.create_calls) == 1
    assert len(backend.bootstrap_calls) == 1
    assert len(backend.skill_calls) == 1


def test_application_restart_and_compression_continue_the_recorded_lineage(tmp_path):
    backend = ControllableSessionBackend()
    storage_root = tmp_path / "plugin-data" / "map-governance"
    first_application = _application(tmp_path, backend, storage_root=storage_root)
    card = _bind(first_application)
    first = first_application.open_map(map_id=card["id"])
    root = first["ceo_session"]["root_session_id"]
    root_record = backend.records[root]
    root_config = backend.session_configs[root]
    backend.records[root] = replace(root_record, end_reason="compression", title=None)
    backend.add_existing(
        "compression-tip",
        title=canonical_session_title(MAP_ID),
        messages=("compressed decision history", "decision after restart"),
        parent_session_id=root,
        started_at="2026-08-23T09:00:00Z",
        last_activity_at="2026-08-23T09:05:00Z",
        system_prompt=root_config[0],
        toolset_fingerprint=root_config[1],
    )

    restarted = _application(tmp_path, backend, storage_root=storage_root)
    reopened = restarted.open_map(map_id=card["id"])

    assert reopened["ceo_session"] == {
        "state": "ready",
        "root_session_id": root,
        "live_session_id": "compression-tip",
        "last_activity_at": "2026-08-23T09:05:00Z",
    }
    assert backend.messages[root] + backend.messages["compression-tip"] == [
        backend.bootstrap_calls[0]["content"],
        backend.skill_calls[0]["content"],
        "compressed decision history",
        "decision after restart",
        backend.skill_calls[1]["content"],
    ]
    assert len(backend.create_calls) == 1
    assert len(backend.bootstrap_calls) == 1
    assert [call["session_id"] for call in backend.skill_calls] == [
        root,
        "compression-tip",
    ]


def test_board_activity_never_rewrites_existing_prompt_or_toolset(tmp_path):
    backend = ControllableSessionBackend()
    title = canonical_session_title(MAP_ID)
    backend.add_existing("existing-root", title=title)
    tracker = SessionTracker()
    application = _application(tmp_path, backend, tracker=tracker)
    card = _bind(application)
    before = backend.session_configs["existing-root"]

    application.open_map(map_id=card["id"])
    tracker.issue = replace(
        tracker.issue,
        title="A renamed Map after board activity",
        labels=("map", "map-stage/delivery"),
    )
    application.refresh(project_id=card["project"]["id"])
    application.open_map(map_id=card["id"])
    after = backend.session_configs["existing-root"]

    assert after == before
    assert backend.records["existing-root"].title == title
    assert backend.bootstrap_calls == []
    assert len(backend.skill_calls) == 1


def test_missing_recorded_session_recovers_when_backend_lineage_returns(tmp_path):
    backend = ControllableSessionBackend()
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application = _application(tmp_path, backend, storage_root=storage_root)
    card = _bind(application)
    opened = application.open_map(map_id=card["id"])
    root = opened["ceo_session"]["root_session_id"]
    missing_record = backend.records.pop(root)

    with pytest.raises(CEOSessionRepairRequired) as raised:
        application.open_map(map_id=card["id"])

    assert "requires repair" in str(raised.value)
    backend.records[root] = missing_record
    restarted = _application(tmp_path, backend, storage_root=storage_root)
    recovered = restarted.open_map(map_id=card["id"])

    assert recovered["ceo_session"]["root_session_id"] == root
    assert restarted.map_detail(map_id=card["id"])["ceo_session"]["state"] == "ready"
    assert len(backend.create_calls) == 1
    assert len(backend.bootstrap_calls) == 1


def test_card_and_detail_expose_readiness_without_conversation_inventory(tmp_path):
    backend = ControllableSessionBackend()
    title = canonical_session_title(MAP_ID)
    backend.add_existing("canonical", title=title)
    backend.add_existing("unrelated-private-chat", title="Unrelated conversation")
    application = _application(tmp_path, backend)
    card = _bind(application)
    application.open_map(map_id=card["id"])

    board = application.board()
    detail = application.map_detail(map_id=card["id"])
    visible = repr({"board": board, "detail": detail})

    assert "unrelated-private-chat" not in visible
    assert "Unrelated conversation" not in visible
    assert "canonical" not in visible
    assert detail["ceo_session"] == {
        "state": "ready",
        "last_activity_at": "2026-08-23T08:01:00Z",
    }
