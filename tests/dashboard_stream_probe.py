from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


plugin_root = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
sys.path.insert(0, str(plugin_root))
module_path = plugin_root / "dashboard" / "plugin_api.py"
spec = importlib.util.spec_from_file_location(
    "map_governance_stream_probe", module_path
)
if spec is None or spec.loader is None:
    raise RuntimeError("dashboard adapter is not importable")
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)
adapter._ws_upgrade_authorized = lambda _ws: True


class CatchUpApplication:
    board_stream_settings = SimpleNamespace(batch_size=200, poll_seconds=0.01)

    def __init__(self) -> None:
        self.sent = False

    def board_events(self, *, cursor, limit):
        assert limit == 200
        if self.sent:
            return {
                "status": "open",
                "events": [],
                "cursor": cursor,
                "latest_cursor": cursor,
                "has_more": False,
            }
        assert cursor == 7
        self.sent = True
        return {
            "status": "open",
            "events": [
                {
                    "cursor": 8,
                    "event_id": "map:I_atlas_41:v8",
                    "project_id": "PVT_acme_7",
                    "map_id": "I_atlas_41",
                    "type": "map.upserted",
                    "payload_hash": "sha256:known",
                    "payload": {"card": {"id": "I_atlas_41", "stage": "delivery"}},
                    "committed_at": "2026-08-23T07:30:00Z",
                }
            ],
            "cursor": 8,
            "latest_cursor": 8,
            "has_more": False,
        }


class ExpiredApplication:
    board_stream_settings = SimpleNamespace(batch_size=200, poll_seconds=0.01)

    def board_events(self, *, cursor, limit):
        return {
            "status": "refresh_required",
            "reason": "cursor_expired",
            "cursor": cursor,
            "retained_after_cursor": 20,
            "latest_cursor": 24,
        }


class ReconnectApplication:
    board_stream_settings = SimpleNamespace(batch_size=200, poll_seconds=0.01)

    def __init__(self) -> None:
        self.phase = 0

    def board_events(self, *, cursor, limit):
        assert limit == 200
        if cursor == 7 and self.phase == 0:
            self.phase = 1
            return self._batch(cursor=8)
        if cursor == 8 and self.phase == 2:
            self.phase = 3
            return self._batch(cursor=9)
        return {
            "status": "open",
            "events": [],
            "cursor": cursor,
            "latest_cursor": cursor,
            "has_more": False,
        }

    @staticmethod
    def _batch(*, cursor):
        return {
            "status": "open",
            "events": [
                {
                    "cursor": cursor,
                    "event_id": f"map:I_atlas_41:v{cursor}",
                    "project_id": "PVT_acme_7",
                    "map_id": "I_atlas_41",
                    "type": "map.upserted",
                    "payload_hash": f"sha256:v{cursor}",
                    "payload": {"version": cursor},
                    "committed_at": "2026-08-23T07:30:00Z",
                }
            ],
            "cursor": cursor,
            "latest_cursor": cursor,
            "has_more": False,
        }


class SlowConsumerApplication:
    board_stream_settings = SimpleNamespace(batch_size=1, poll_seconds=0.01)

    def board_events(self, *, cursor, limit):
        assert limit == 1
        if cursor < 9:
            next_cursor = cursor + 1
            return {
                **ReconnectApplication._batch(cursor=next_cursor),
                "latest_cursor": 9,
                "has_more": next_cursor < 9,
            }
        return {
            "status": "open",
            "events": [],
            "cursor": cursor,
            "latest_cursor": 9,
            "has_more": False,
        }


applications = {
    "catch-up": CatchUpApplication,
    "expired": ExpiredApplication,
    "reconnect": ReconnectApplication,
    "slow": SlowConsumerApplication,
}
application = applications[mode]()
adapter.application_for_profile = lambda _profile: application
api = FastAPI()
api.include_router(adapter.router, prefix="/api/plugins/map-governance")

with TestClient(api) as client:
    cursor = 3 if mode == "expired" else 7
    with client.websocket_connect(
        f"/api/plugins/map-governance/events?profile=ceo&cursor={cursor}"
    ) as socket:
        first = socket.receive_json()
        if mode in {"catch-up", "reconnect", "slow"}:
            assert first == {
                "status": "open",
                "cursor": 7,
                "latest_cursor": 9 if mode == "slow" else 8,
            }
            assert socket.receive_json()["events"][0]["cursor"] == 8
            if mode == "slow":
                assert socket.receive_json()["events"][0]["cursor"] == 9
        else:
            assert first == {
                "status": "refresh_required",
                "reason": "cursor_expired",
                "cursor": 3,
                "retained_after_cursor": 20,
                "latest_cursor": 24,
            }
    if mode == "reconnect":
        application.phase = 2
        with client.websocket_connect(
            "/api/plugins/map-governance/events?profile=ceo&cursor=8"
        ) as socket:
            assert socket.receive_json() == {
                "status": "open",
                "cursor": 8,
                "latest_cursor": 9,
            }
            assert socket.receive_json()["events"][0]["cursor"] == 9

print(f"dashboard stream {mode} ready")
