"""제작 비용 누적 원장(ledger).

사이클마다 `nutti run`은 별도 프로세스이므로, "이번 달 얼마 썼나"를 알려면 각 실행의
비용을 프로세스 밖에 누적해야 한다. 여기서는 외부 의존성 없이 로컬 JSON 파일에
한 줄(한 실행)씩 기록하고, `nutti cost`가 이를 일/월/전체로 합산한다.

- dry_run 실행은 **실제 지출 0**으로 기록하되 라이브였다면 들었을 예상치(total_usd)도 남긴다.
- 게이트 거절/팩트체크 실패로 중단된 실행은 `run.cost`가 없어 기록되지 않는다
  (영상까지 만들고 검수에서 막힌 경우의 부분 지출은 v1 미집계 — 한계).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from nutti.logging import get_logger
from nutti.models import PipelineRun, _utcnow

log = get_logger(__name__)


class CostLedger:
    """실행별 비용을 JSON 배열 파일에 누적하는 경량 영속 원장.

    파일이 없거나 깨져 있어도 죽지 않고 빈 원장으로 폴백한다(PipelineState와 동일 패턴).
    """

    def __init__(self, path: str):
        self.path = Path(path)

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as exc:
            if not isinstance(exc, FileNotFoundError):
                log.warning("cost.ledger.load.failed", path=str(self.path), error=str(exc))
            return []
        return data if isinstance(data, list) else []

    def _save(self, records: list[dict]) -> None:
        # 원자적 쓰기(tmp 작성 후 os.replace) — 크래시·동시 실행 시 파일 손상 방지.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(records, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def record(self, run: PipelineRun) -> None:
        """완주한 실행의 비용을 원장에 한 줄 추가한다(run.cost 없으면 무시)."""
        if run.cost is None:
            return
        cost = run.cost
        actual = 0.0 if cost.dry_run else cost.total_usd
        record = {
            "run_id": run.id,
            "topic": run.topic,
            "recorded_at": _utcnow().isoformat(),
            "total_usd": round(cost.total_usd, 4),   # 라이브 기준 비용(=실측+추정)
            "actual_usd": round(actual, 4),          # 실제 지출(dry_run이면 0)
            "dry_run": cost.dry_run,
        }
        records = self._load()
        records.append(record)
        self._save(records)
        log.info(
            "cost.ledger.recorded",
            run_id=run.id,
            actual_usd=record["actual_usd"],
            dry_run=cost.dry_run,
        )

    def records(self) -> list[dict]:
        """원장의 모든 기록(최신 추가가 뒤)."""
        return self._load()


def _to_local(iso: str) -> datetime | None:
    """ISO 문자열(UTC, tz-aware)을 로컬 시간대로 변환한다(파싱 실패 시 None)."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    # tz-aware면 로컬로 변환, naive면 그대로(레거시 폴백).
    return dt.astimezone() if dt.tzinfo is not None else dt


def summarize_records(
    records: list[dict], *, now: datetime, days: int | None = None
) -> dict:
    """원장 기록을 오늘/이번 달/전체(+선택 최근 N일)로 합산한다.

    각 버킷은 {actual, estimated, runs}를 담는다.
    - actual: 실제 지출 합계(dry_run 제외분의 실지출).
    - estimated: dry_run 실행이 라이브였다면 들었을 예상 비용 합계(참고용).
    - runs: 해당 기간 기록 수.

    `now`는 로컬 시간 기준(테스트 결정성을 위해 주입). 일/월 경계도 로컬 기준이다.
    """
    today = now.date()
    ym = (now.year, now.month)
    cutoff = now.timestamp() - days * 86400 if days else None

    def _empty() -> dict:
        return {"actual": 0.0, "estimated": 0.0, "runs": 0}

    buckets = {"today": _empty(), "month": _empty(), "all": _empty()}
    if days:
        buckets["window"] = _empty()

    for rec in records:
        local = _to_local(str(rec.get("recorded_at", "")))
        actual = float(rec.get("actual_usd", 0.0) or 0.0)
        total = float(rec.get("total_usd", 0.0) or 0.0)
        estimated = total if rec.get("dry_run") else 0.0

        def _add(key: str) -> None:
            buckets[key]["actual"] += actual
            buckets[key]["estimated"] += estimated
            buckets[key]["runs"] += 1

        _add("all")
        if local is not None:
            if local.date() == today:
                _add("today")
            if (local.year, local.month) == ym:
                _add("month")
            if cutoff is not None and local.timestamp() >= cutoff:
                _add("window")

    # 부동소수 잔차 정리.
    for bucket in buckets.values():
        bucket["actual"] = round(bucket["actual"], 4)
        bucket["estimated"] = round(bucket["estimated"], 4)
    return buckets


def format_summary(buckets: dict, *, days: int | None = None) -> str:
    """summarize_records 결과를 한국어 요약 문자열로 만든다(CLI 출력용)."""

    def _line(label: str, b: dict) -> str:
        base = f"  {label}: 실제 지출 ${b['actual']:.4f}  ({b['runs']}건)"
        if b["estimated"] > 0:
            base += f"  · dry_run 예상 ${b['estimated']:.4f}"
        return base

    lines = ["[누적 제작 비용]"]
    lines.append(_line("오늘", buckets["today"]))
    lines.append(_line("이번 달", buckets["month"]))
    if days and "window" in buckets:
        lines.append(_line(f"최근 {days}일", buckets["window"]))
    lines.append(_line("전체 누적", buckets["all"]))
    return "\n".join(lines)
