"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _hermetic_pricing(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Pin every test to ``DEFAULT_PRICING`` instead of the developer's config.

    기본 경로가 ``config/pricing.json`` 상대 경로라, 테스트를 리포지토리
    루트에서 돌리면 개발자의 실제 단가 파일이 조용히 끼어든다. 실제로 이 때문에
    opus 단가를 로컬에서 조정한 순간 test_pricing 3건이 깨져 있었다.
    빈 ``models``를 가리켜 파일 부재 경고 없이 기본 단가표만 쓰게 한다.

    예외: ``pricing_gap`` 마커가 붙은 테스트는 운영자의 실제 단가 파일을 읽어야
    한다 — 이 테스트의 목적 자체가 실제 설정에 미등록 모델이 없음을 검증하는
    것이므로, 빈 단가표로 핀하면 테스트가 구조적으로 실패한다.
    그 경우 환경 변수를 건드리지 않고 ``refresh_pricing()``만 호출해
    실제 단가표를 로드한다.
    """
    from app import pricing

    if request.node.get_closest_marker("pricing_gap"):
        pricing.refresh_pricing()
        return

    path = tmp_path_factory.mktemp("pricing") / "pricing.json"
    path.write_text('{"models": {}}', encoding="utf-8")
    monkeypatch.setenv("USAGE_PRICING_FILE", str(path))
    pricing.refresh_pricing()


@pytest.fixture(autouse=True)
def _hermetic_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Pin registry reads to an empty directory instead of the developer's registry."""
    registry_dir = tmp_path_factory.mktemp("registry")
    monkeypatch.setenv("USAGE_REGISTRY_DIR", str(registry_dir))


@pytest.fixture(autouse=True)
def _hermetic_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """테스트에서 실 ANTHROPIC_API_KEY가 새어들지 못하게 막는다.

    키가 export된 개발 머신에서 _inject 없는 quiz 테스트가 실 과금 호출을 하는
    사고 방지 — client_from_env()를 몽키패치하지 않은 테스트는 항상 키 부재로
    떨어져야 한다.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture()
def fixtures_dir() -> Path:
    """Return the path to the tests/fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture()
def client() -> TestClient:
    """Return a FastAPI TestClient for the application.

    The app module is imported lazily so tests can run even before
    app/main.py exists (the import failure itself is part of the RED phase).
    """
    from app.main import app

    return TestClient(app)
