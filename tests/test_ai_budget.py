from datetime import UTC, datetime
from pathlib import Path

from common.ai import ai_budget_status
from common.config import AISettings
from common.database import Database


def test_ai_budget_status_reports_usage(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    now = datetime.now(UTC)
    db.record_ai_usage(purpose="score", model="claude", input_tokens=120, output_tokens=40, created_at=now)
    db.record_ai_usage(purpose="write", model="claude", input_tokens=300, output_tokens=90, created_at=now)

    status = ai_budget_status(
        db,
        AISettings(max_daily_requests=10, max_daily_input_tokens=1000, max_daily_output_tokens=500),
        now=now,
    )

    assert status["requests"] == 2
    assert status["input_tokens"] == 420
    assert status["by_purpose"]["write"]["output_tokens"] == 90
