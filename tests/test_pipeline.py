"""dry_run 파이프라인 스모크 테스트.

외부 API 키 없이 전 단계가 끝까지 돌고, 검수 게이트가 동작하는지 검증한다.
"""

from __future__ import annotations

import pytest

from nutti.config import Settings
from nutti.integrations.ai_text import FactCheckResult
from nutti.models import ContentFormat, ReviewDecision, ReviewRequest, Stage
from nutti.pipeline.orchestrator import FactCheckFailed, GateRejected, Orchestrator
from nutti.review.gates import AutoApproveGate


def _dry_settings() -> Settings:
    return Settings(NUTTI_DRY_RUN=True, NUTTI_ENV="test")


def test_full_run_dry_run():
    orch = Orchestrator(
        _dry_settings(),
        telegram=AutoApproveGate(),
        discord=AutoApproveGate(),
    )
    run = orch.run("강아지 닭가슴살 간식 적정량")

    assert run.script is not None and run.script.body
    assert run.video is not None and run.video.final_url
    assert run.metadata is not None and run.metadata.title
    assert len(run.uploads) == 1  # shorts → youtube만
    assert run.uploads[0].platform == "youtube"
    assert run.current_stage == Stage.ANALYTICS


def test_reels_uploads_both():
    orch = Orchestrator(
        _dry_settings(),
        telegram=AutoApproveGate(),
        discord=AutoApproveGate(),
    )
    run = orch.run("강아지 수제간식", content_format=ContentFormat.REELS)
    platforms = {u.platform for u in run.uploads}
    assert platforms == {"youtube", "instagram"}


def test_analysis_feedback_loop():
    orch = Orchestrator(_dry_settings(), telegram=AutoApproveGate(), discord=AutoApproveGate())
    run = orch.run("강아지 간식")
    analysis = orch.collect_and_analyze(run)
    assert isinstance(analysis, str) and analysis
    assert run.reports and run.reports[0].views > 0


class _RejectGate:
    def request(self, review: ReviewRequest) -> ReviewDecision:
        return ReviewDecision.REJECTED


def test_gate_rejection_stops_pipeline():
    orch = Orchestrator(_dry_settings(), telegram=_RejectGate(), discord=AutoApproveGate())
    try:
        orch.run("부적절한 주제")
        assert False, "검수 거절 시 GateRejected가 발생해야 한다"
    except GateRejected as exc:
        assert exc.stage == Stage.SCRIPT
        assert exc.decision == ReviewDecision.REJECTED


# --- 팩트체크 배선(#1): 오케스트레이터가 fact_check_script를 실제로 호출하는지 ---

def _approving_orch(max_retries: int = 1) -> Orchestrator:
    return Orchestrator(
        _dry_settings(),
        telegram=AutoApproveGate(),
        discord=AutoApproveGate(),
        max_factcheck_retries=max_retries,
    )


def _log_counter(store, monkeypatch):
    """store.log_script 호출 횟수를 세는 카운터를 설치하고 반환한다."""
    n = {"count": 0}

    def _logged(_script):
        n["count"] += 1

    monkeypatch.setattr(store, "log_script", _logged)
    return n


def test_factcheck_is_wired_and_passes(monkeypatch):
    orch = _approving_orch()
    calls = {"n": 0}

    def passing(_script):
        calls["n"] += 1
        return FactCheckResult(passed=True, issues=[])

    monkeypatch.setattr(orch.ai, "fact_check_script", passing)
    logged = _log_counter(orch.store, monkeypatch)
    run = orch.run("안전한 주제")
    assert calls["n"] == 1  # 호출됨(데드코드 아님)
    assert run.script.fact_checked is True
    assert logged["count"] == 1  # 통과 시 대본 기록됨
    assert run.uploads  # 정상 진행


def test_factcheck_regenerates_with_issue_feedback_then_rejects(monkeypatch):
    orch = _approving_orch(max_retries=1)
    fc_calls = {"n": 0}
    feedbacks: list[str] = []

    def always_fail(_script):
        fc_calls["n"] += 1
        return FactCheckResult(passed=False, issues=["근거 없는 효능 주장"])

    real_gen = orch.ai.generate_script

    def capturing_gen(topic, feedback=""):
        feedbacks.append(feedback)
        return real_gen(topic, feedback=feedback)

    monkeypatch.setattr(orch.ai, "fact_check_script", always_fail)
    monkeypatch.setattr(orch.ai, "generate_script", capturing_gen)
    logged = _log_counter(orch.store, monkeypatch)

    with pytest.raises(FactCheckFailed) as exc:
        orch.run("위험한 주제")
    assert exc.value.issues == ["근거 없는 효능 주장"]
    assert fc_calls["n"] == 2  # 최초 + 재생성 1회
    assert len(feedbacks) == 2  # 재생성으로 대본 다시 생성됨
    # 재생성 피드백에 팩트체크 지적이 실제로 담겨야 한다(빈 호출 방지).
    assert "근거 없는 효능 주장" in feedbacks[1]
    assert logged["count"] == 0  # 거절된 대본은 기록 안 됨


def test_factcheck_zero_retries_fails_immediately(monkeypatch):
    orch = _approving_orch(max_retries=0)
    fc_calls = {"n": 0}

    def always_fail(_script):
        fc_calls["n"] += 1
        return FactCheckResult(passed=False, issues=["문제"])

    monkeypatch.setattr(orch.ai, "fact_check_script", always_fail)
    with pytest.raises(FactCheckFailed):
        orch.run("주제")
    assert fc_calls["n"] == 1  # 재생성 없이 최초 1회 후 즉시 거절


def test_factcheck_passes_after_one_retry(monkeypatch):
    orch = _approving_orch(max_retries=2)
    results = iter([
        FactCheckResult(passed=False, issues=["수정 필요"]),
        FactCheckResult(passed=True, issues=[]),
    ])
    monkeypatch.setattr(orch.ai, "fact_check_script", lambda _s: next(results))
    run = orch.run("주제")
    assert run.script.fact_checked is True
    assert run.uploads  # 재생성 후 통과 → 정상 진행
