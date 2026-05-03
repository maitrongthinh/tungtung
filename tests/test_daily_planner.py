from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from common.database import Database
from modules.memory.daily_planner import DailyPlanner


class FakeImprovementUpdater:
    def __init__(self, database: Database) -> None:
        self.database = database

    def category_watch_list(self) -> list[str]:
        return ["thời trang nữ", "gia dụng thông minh"]


def test_daily_planner_generates_markdown(tmp_path: Path, monkeypatch) -> None:
    fake_settings = SimpleNamespace(
        memory_dir=tmp_path / "memory",
        accounts_dir=tmp_path / "accounts",
        kpi=SimpleNamespace(posts_per_day=20, draft_buffer=25),
        meta=SimpleNamespace(window_a_start="11:00", window_a_end="13:00", window_b_start="20:00", window_b_end="22:00"),
    )
    fake_settings.memory_dir.mkdir(parents=True, exist_ok=True)
    fake_settings.accounts_dir.mkdir(parents=True, exist_ok=True)
    (fake_settings.accounts_dir / "acc_001.json").write_text(
        '{"id":"acc_001","page_id":"1","access_token":"token","token_expires_at":"2026-12-31","page_name":"Page","niche":"thời trang nữ","tone":"thân thiện"}',
        encoding="utf-8",
    )
    monkeypatch.setattr("modules.memory.daily_planner.load_settings", lambda *args, **kwargs: fake_settings)
    monkeypatch.setattr("modules.memory.daily_planner.ImprovementUpdater", FakeImprovementUpdater)
    db = Database(tmp_path / "test.db")
    planner = DailyPlanner(db)
    path = planner.generate(datetime.now(UTC))
    content = path.read_text(encoding="utf-8")
    assert "# Daily Plan" in content
    assert "Category Watch List" in content
