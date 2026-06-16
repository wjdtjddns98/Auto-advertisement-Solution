"""누적 비용 원장(CostLedger) + 일/월/전체 합산(summarize_records) 테스트.

외부 호출 없이 도메인 모델·로컬 JSON 파일만으로 동작한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nutti.config import Settings
from nutti.models import CostBreakdown, CostLineItem, PipelineRun
from nutti.pipeline.cost_ledger import (
    CostLedger,
    format_summary,
    summarize_records,
)
from nutti.pipeline.orchestrator import Orchestrator
from nutti.review.gates import AutoApproveGate


def _run_with_cost(total: float, *, dry_run: bool, topic: str = "강아지 간식") -> PipelineRun:
    run = PipelineRun(topic=topic)
    run.cost = CostBreakdown(
        items=[CostLineItem(label="영상", usd=total)],
        total_usd=total,
        dry_run=dry_run,
    )
    return run


def test_record_dry_run_has_zero_actual(tmp_path):
    ledger = CostLedger(str(tmp_path / "ledger.json"))
    ledger.record(_run_with_cost(2.95, dry_run=True))
    recs = ledger.records()
    assert len(recs) == 1
    assert recs[0]["actual_usd"] == 0.0       # dry_run은 실제 지출 0
    assert recs[0]["total_usd"] == 2.95        # 라이브 예상치는 보존
    assert recs[0]["dry_run"] is True


def test_record_live_counts_actual(tmp_path):
    ledger = CostLedger(str(tmp_path / "ledger.json"))
    ledger.record(_run_with_cost(3.25, dry_run=False))
    assert ledger.records()[0]["actual_usd"] == 3.25


def test_record_skips_run_without_cost(tmp_path):
    ledger = CostLedger(str(tmp_path / "ledger.json"))
    ledger.record(PipelineRun(topic="중단된 실행"))  # run.cost is None
    assert ledger.records() == []


def test_record_appends_across_calls(tmp_path):
    ledger = CostLedger(str(tmp_path / "ledger.json"))
    ledger.record(_run_with_cost(1.0, dry_run=False))
    ledger.record(_run_with_cost(2.0, dry_run=False))
    assert len(ledger.records()) == 2


def test_corrupt_file_falls_back_to_empty(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not valid json", encoding="utf-8")
    ledger = CostLedger(str(path))
    assert ledger.records() == []
    # 손상돼 있어도 새 기록은 정상 추가(폴백 후 덮어쓰기).
    ledger.record(_run_with_cost(1.0, dry_run=False))
    assert len(ledger.records()) == 1


# --- summarize_records: 기간별 합산 ---

def _rec(recorded_at: datetime, *, actual: float, total: float, dry_run: bool) -> dict:
    return {
        "run_id": "x",
        "topic": "t",
        "recorded_at": recorded_at.isoformat(),
        "total_usd": total,
        "actual_usd": actual,
        "dry_run": dry_run,
    }


def test_summary_buckets_today_month_all():
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc).astimezone()
    records = [
        _rec(now, actual=3.0, total=3.0, dry_run=False),                    # 오늘·이번달
        _rec(now - timedelta(days=2), actual=2.0, total=2.0, dry_run=False),  # 이번달(오늘 아님)
        _rec(now - timedelta(days=40), actual=5.0, total=5.0, dry_run=False),  # 전월
    ]
    b = summarize_records(records, now=now)
    assert b["today"]["actual"] == 3.0 and b["today"]["runs"] == 1
    assert b["month"]["actual"] == 5.0 and b["month"]["runs"] == 2
    assert b["all"]["actual"] == 10.0 and b["all"]["runs"] == 3


def test_summary_separates_dry_run_estimated():
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc).astimezone()
    records = [
        _rec(now, actual=0.0, total=2.95, dry_run=True),    # 예상만
        _rec(now, actual=3.25, total=3.25, dry_run=False),  # 실제
    ]
    b = summarize_records(records, now=now)
    assert b["today"]["actual"] == 3.25
    assert b["today"]["estimated"] == 2.95
    assert b["today"]["runs"] == 2


def test_summary_window_days():
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc).astimezone()
    records = [
        _rec(now - timedelta(days=1), actual=1.0, total=1.0, dry_run=False),
        _rec(now - timedelta(days=10), actual=9.0, total=9.0, dry_run=False),
    ]
    b = summarize_records(records, now=now, days=7)
    assert "window" in b
    assert b["window"]["actual"] == 1.0  # 최근 7일 내 1건만
    assert b["all"]["actual"] == 10.0


def test_summary_ignores_unparseable_dates():
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc).astimezone()
    records = [{"recorded_at": "garbage", "actual_usd": 5.0, "total_usd": 5.0, "dry_run": False}]
    b = summarize_records(records, now=now)
    assert b["all"]["actual"] == 5.0   # 전체엔 잡히고
    assert b["today"]["runs"] == 0     # 날짜 버킷엔 안 잡힘


def test_format_summary_shows_actual_and_estimate():
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc).astimezone()
    b = summarize_records(
        [_rec(now, actual=0.0, total=2.95, dry_run=True)], now=now
    )
    text = format_summary(b)
    assert "누적 제작 비용" in text
    assert "오늘" in text and "이번 달" in text
    assert "dry_run 예상 $2.9500" in text


# --- 오케스트레이터 배선 ---

def test_orchestrator_records_to_ledger(tmp_path):
    settings = Settings(
        NUTTI_DRY_RUN=True,
        NUTTI_ENV="test",
        NUTTI_COST_LEDGER_PATH=str(tmp_path / "ledger.json"),
    )
    ledger = CostLedger(settings.cost_ledger_path)
    orch = Orchestrator(
        settings, telegram=AutoApproveGate(), discord=AutoApproveGate(), ledger=ledger
    )
    orch.run("강아지 닭가슴살 간식 적정량")
    recs = ledger.records()
    assert len(recs) == 1
    assert recs[0]["dry_run"] is True
    assert recs[0]["total_usd"] > 0
    assert recs[0]["actual_usd"] == 0.0  # dry_run → 실제 지출 0
