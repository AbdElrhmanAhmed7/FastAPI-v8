"""Shared pytest fixtures.

Every test gets its own SQLite file and its own application instance, so tests
are isolated and can run in any order. NASA access is disabled by default –
the suite must pass on a plane.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import Settings  # noqa: E402
from app.domain.state import GameState  # noqa: E402
from app.main import create_app  # noqa: E402
from app.missions.registry import GameContent, load_content  # noqa: E402
from app.services.game_service import create_initial_state  # noqa: E402


@pytest.fixture(scope="session")
def content() -> GameContent:
    """The bundled content pack (immutable, loaded once)."""
    return load_content(ROOT / "content")


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "test",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'nova-test.db'}",
        "auto_create_tables": True,
        "nasa_enabled": False,
        "rate_limit_enabled": False,
        "auth_enabled": False,
        "log_json": False,
        "log_level": "ERROR",
        "content_dir": ROOT / "content",
        "secret_key": "test-secret",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def module_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """One app for a whole module – for suites that need a long campaign."""
    settings = make_settings(tmp_path_factory.mktemp("nova-module"))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def campaign(module_client: TestClient) -> Any:
    """A fully played campaign (every mission completed) driven over HTTP.

    Late-stage endpoints (science, Earth trends, simulator history) are only
    reachable after the chain unlocks, so the suite pays for one real playthrough
    instead of faking state. Uses the same public API a game client would.
    """
    from autopilot import Autopilot

    pilot = Autopilot(client=module_client)
    report = pilot.run_campaign(player_name="Fixture Commander", difficulty="standard", seed=11)
    return SimpleNamespace(
        pilot=pilot,
        client=module_client,
        game_id=pilot.game_id,
        token=pilot.session_token,
        report=report,
        api=Api(module_client, pilot.game_id, pilot.session_token),
    )


@pytest.fixture
def auth_client(tmp_path: Path) -> Iterator[TestClient]:
    """An app with session-token authentication enabled."""
    with TestClient(create_app(make_settings(tmp_path, auth_enabled=True))) as test_client:
        yield test_client


@pytest.fixture
def game(client: TestClient) -> dict[str, Any]:
    """A fresh campaign: returns the full create-game response."""
    response = client.post(
        "/api/v1/games", json={"player_name": "Test Commander", "difficulty": "standard", "seed": 7}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def game_id(game: dict[str, Any]) -> str:
    return str(game["game_id"])


@pytest.fixture
def state(content: GameContent) -> GameState:
    """A pure in-memory save document for domain-level tests."""
    from app.domain.enums import Difficulty
    from app.missions.engine import MissionEngine

    document = create_initial_state(
        content, player_name="Unit", difficulty=Difficulty.standard, seed=7, game_id="nova_unit"
    )
    MissionEngine(content).bootstrap(document)
    return document


class Api:
    """Tiny convenience wrapper so tests read like the game flow."""

    def __init__(self, client: TestClient, game_id: str, token: str = "") -> None:
        self.client = client
        self.game_id = game_id
        self.headers = {"X-Session-Token": token} if token else {}

    def url(self, path: str) -> str:
        return f"/api/v1/games/{self.game_id}{path}"

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.client.get(self.url(path), headers=self.headers, **kwargs)

    def post(self, path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return self.client.post(self.url(path), json=payload or {}, headers=headers, **kwargs)

    def start(self, mission_id: str) -> Any:
        return self.post(f"/missions/{mission_id}/start")

    def act(
        self, mission_id: str, action: str, payload: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return self.post(
            f"/missions/{mission_id}/actions",
            {"action": action, "payload": payload or {}},
            **kwargs,
        )

    def mission(self, mission_id: str) -> Any:
        return self.get(f"/missions/{mission_id}")


@pytest.fixture
def api(client: TestClient, game: dict[str, Any]) -> Api:
    return Api(client, game["game_id"], game["session_token"])


def complete_training(api: Api) -> None:
    """Walk the Training Facility so later missions unlock with a good loadout."""
    api.start("training_prep")
    for action, payload in (
        ("review_brief", {}),
        ("collect_oxygen", {}),
        ("take_repair_tools", {}),
        ("take_scanner", {}),
        ("inspect_rover", {"check_mode": "full"}),
        ("suit_check", {}),
        ("confirm_readiness", {}),
    ):
        response = api.act("training_prep", action, payload)
        assert response.status_code == 200, response.text


def complete_systems_check(api: Api) -> None:
    api.start("analog_systems_check")
    for action in ("check_ventilation", "check_power_bus", "check_water_loop", "log_system_report"):
        response = api.act("analog_systems_check", action)
        assert response.status_code == 200, response.text
