from __future__ import annotations

from multiprocessing import get_context
from pathlib import Path

from map_governance import MapGovernanceApplication
from map_governance.sessions import (
    HermesSessionAdapter,
    HermesSessionDatabaseBackend,
    canonical_session_identity,
    canonical_session_title,
)
from map_governance.tracker import TrackerIssue, TrackerProject


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ISSUE_URL = "https://github.com/acme/atlas/issues/41"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"


class _BindingTracker:
    def get_project(self, url: str) -> TrackerProject:
        assert url == PROJECT_URL
        return TrackerProject(
            id="PVT_acme_7",
            owner="acme",
            owner_type="organization",
            number=7,
            title="Acme CEO portfolio",
            url=url,
        )

    def get_issue(self, url: str) -> TrackerIssue:
        assert url == ISSUE_URL
        return TrackerIssue(
            id="I_atlas_41",
            repository="acme/atlas",
            number=41,
            title="Map the Atlas launch",
            url=url,
            state="open",
            state_reason=None,
            labels=("map", "map-stage/authorized"),
        )

    def transition_issue_stage(self, *args, **kwargs):
        raise AssertionError("session tests do not transition tracker state")


def _open_real_session_in_process(
    storage_root: str,
    state_database: str,
    barrier,
    results,
) -> None:
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=Path(storage_root),
        session_runner=HermesSessionAdapter(
            HermesSessionDatabaseBackend(Path(state_database))
        ),
        profile_name="ceo",
    )
    barrier.wait()
    opened = application.open_map(map_id="I_atlas_41")
    results.put(opened["ceo_session"]["root_session_id"])


def test_real_hermes_session_contract_mints_bootstraps_and_reopens(
    tmp_path, hermes_host_root
):
    from hermes_state import SessionDB

    state_database = tmp_path / "hermes-home" / "profiles" / "ceo" / "state.db"
    backend = HermesSessionDatabaseBackend(state_database)
    adapter = HermesSessionAdapter(backend)
    identity = canonical_session_identity(profile_name="ceo", map_id="I_atlas_41")
    title = canonical_session_title("I_atlas_41")
    bootstrap = "canonical bootstrap for https://github.com/acme/atlas/issues/41"

    minted = adapter.mint(
        identity=identity,
        title=title,
        profile_name="ceo",
        bootstrap=bootstrap,
        idempotency_key=f"{identity}:bootstrap",
    )

    assert minted.root_session_id == minted.live_session_id
    assert minted.bootstrap_sent is True
    with SessionDB(db_path=state_database) as database:
        row = database.get_session(minted.root_session_id)
        messages = database.get_messages(minted.root_session_id)
    assert row["title"] == title
    assert row["profile_name"] == "ceo"
    assert row["source"] == "desktop"
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", bootstrap)
    ]

    restarted = HermesSessionAdapter(HermesSessionDatabaseBackend(state_database))
    adopted = restarted.find_exact(title=title)
    initialized = restarted.initialize(
        adopted[0],
        bootstrap="must not be appended",
        idempotency_key=f"{identity}:bootstrap:retry",
    )

    assert len(adopted) == 1
    assert initialized.root_session_id == minted.root_session_id
    assert initialized.bootstrap_sent is False
    with SessionDB(db_path=state_database) as database:
        assert database.get_session(minted.root_session_id)["message_count"] == 1


def test_real_hermes_compression_contract_preserves_lineage_and_prompt_bytes(
    tmp_path, hermes_host_root
):
    from hermes_state import SessionDB

    state_database = tmp_path / "hermes-home" / "profiles" / "ceo" / "state.db"
    backend = HermesSessionDatabaseBackend(state_database)
    adapter = HermesSessionAdapter(backend)
    identity = canonical_session_identity(profile_name="ceo", map_id="I_atlas_41")
    title = canonical_session_title("I_atlas_41")
    root = adapter.mint(
        identity=identity,
        title=title,
        profile_name="ceo",
        bootstrap="bootstrap",
        idempotency_key=f"{identity}:bootstrap",
    ).root_session_id
    stable_prompt = "byte-stable CEO system prompt\nfixed toolset"
    stable_model_config = {"toolsets": ["map-governance-ceo"], "temperature": 0}

    with SessionDB(db_path=state_database) as database:
        database.create_session(
            root,
            source="desktop",
            profile_name="ceo",
            system_prompt=stable_prompt,
            model_config=stable_model_config,
        )
        before = database.get_session(root)
        database.end_session(root, "compression")
        database.create_session(
            "compression-tip",
            source="desktop",
            profile_name="ceo",
            parent_session_id=root,
            system_prompt=stable_prompt,
            model_config=stable_model_config,
        )
        database.append_message(
            "compression-tip",
            role="assistant",
            content="decision history survived compression",
        )
        database.set_session_title("compression-tip", title)

    resolved = HermesSessionAdapter(
        HermesSessionDatabaseBackend(state_database)
    ).resolve(root_session_id=root)

    assert resolved is not None
    assert resolved.root_session_id == root
    assert resolved.live_session_id == "compression-tip"
    with SessionDB(db_path=state_database) as database:
        root_after = database.get_session(root)
        tip_after = database.get_session("compression-tip")
        lineage = database.get_messages_as_conversation(
            "compression-tip", include_ancestors=True
        )
    assert root_after["system_prompt"] == before["system_prompt"] == stable_prompt
    assert tip_after["system_prompt"] == stable_prompt
    assert root_after["model_config"] == before["model_config"]
    assert tip_after["model_config"] == before["model_config"]
    assert [message["content"] for message in lineage] == [
        "bootstrap",
        "decision history survived compression",
    ]


def test_real_hermes_profile_databases_keep_same_map_sessions_isolated(
    tmp_path, hermes_host_root
):
    title = canonical_session_title("I_atlas_41")
    roots = []

    for profile in ("ceo", "chairman"):
        database = (
            tmp_path / "hermes-home" / "profiles" / profile / "state.db"
        )
        adapter = HermesSessionAdapter(HermesSessionDatabaseBackend(database))
        identity = canonical_session_identity(
            profile_name=profile,
            map_id="I_atlas_41",
        )
        roots.append(
            adapter.mint(
                identity=identity,
                title=title,
                profile_name=profile,
                bootstrap=f"bootstrap for {profile}",
                idempotency_key=f"{identity}:bootstrap",
            ).root_session_id
        )
        assert len(adapter.find_exact(title=title)) == 1

    assert roots[0] != roots[1]


def test_real_hermes_concurrent_processes_converge_and_bootstrap_once(
    tmp_path, hermes_host_root
):
    from hermes_state import SessionDB

    # Establish only the Map projection; each child runs the production
    # adapter's exact-adopt/mint/bootstrap flow against shared durable stores.
    storage_root = tmp_path / "plugin-data" / "map-governance"
    setup = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=_BindingTracker(),
    )
    project = setup.configure_project(project_url=PROJECT_URL)
    setup.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    state_database = tmp_path / "hermes-home" / "profiles" / "ceo" / "state.db"
    process_context = get_context("spawn")
    barrier = process_context.Barrier(4)
    results = process_context.Queue()
    processes = [
        process_context.Process(
            target=_open_real_session_in_process,
            args=(str(storage_root), str(state_database), barrier, results),
        )
        for _ in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    roots = [results.get(timeout=2) for _ in processes]
    assert len(set(roots)) == 1
    with SessionDB(db_path=state_database) as database:
        assert database.get_session(roots[0])["message_count"] == 1
