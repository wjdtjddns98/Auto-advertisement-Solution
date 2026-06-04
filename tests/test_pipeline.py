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
from nutti.storage.state_store import PipelineState


def _dry_settings() -> Settings:
    return Settings(NUTTI_DRY_RUN=True, NUTTI_ENV="test")


def _tmp_state(tmp_path) -> PipelineState:
    """테스트가 리포지토리의 data/를 건드리지 않도록 tmp 경로 상태를 만든다."""
    return PipelineState(str(tmp_path / "state.json"))


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """모든 테스트가 기본 상태 경로 대신 tmp를 쓰도록 격리(리포지토리 data/ 오염 방지).

    state=를 명시 주입하지 않는 Orchestrator(예: _approving_orch)도 이 경로를 따른다.
    """
    monkeypatch.setenv("NUTTI_STATE_PATH", str(tmp_path / "default_state.json"))


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


def test_analysis_feedback_loop(tmp_path):
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), discord=AutoApproveGate(), state=state
    )
    run = orch.run("강아지 간식")
    analysis = orch.collect_and_analyze(run)
    assert isinstance(analysis, str) and analysis
    assert run.reports and run.reports[0].views > 0
    # 피드백 루프: 분석 결과가 상태에 저장돼 다음 사이클로 자동 연결돼야 한다.
    assert state.get_feedback() == analysis


# --- 피드백 자동 연결 + 주제 자동 생성(resolve_inputs) ---

def test_resolve_inputs_auto_loads_saved_feedback(tmp_path):
    """feedback 미지정 시 직전 사이클이 저장한 분석을 자동으로 불러온다."""
    state = _tmp_state(tmp_path)
    state.save_feedback("Q&A 포맷 지속률 우수 → 비중 확대")
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), discord=AutoApproveGate(), state=state
    )
    topic, feedback = orch.resolve_inputs("명시 주제", "")
    assert topic == "명시 주제"
    assert feedback == "Q&A 포맷 지속률 우수 → 비중 확대"


def test_resolve_inputs_explicit_feedback_wins(tmp_path):
    """명시한 feedback이 저장된 값보다 우선한다."""
    state = _tmp_state(tmp_path)
    state.save_feedback("저장된 피드백")
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), discord=AutoApproveGate(), state=state
    )
    _, feedback = orch.resolve_inputs("주제", "명시 피드백")
    assert feedback == "명시 피드백"


def test_resolve_inputs_auto_generates_topic_when_omitted(tmp_path):
    """주제 미지정 시 자동 생성하고, 최근 주제에 기록한다."""
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), discord=AutoApproveGate(), state=state
    )
    topic, _ = orch.resolve_inputs(None, "")
    assert topic  # 비어있지 않은 자동 생성 주제
    assert state.get_recent_topics()[0] == topic  # 최신 주제로 기록됨


def test_resolve_inputs_auto_topic_avoids_recent(tmp_path):
    """연속 자동 생성 시 직전 주제와 겹치지 않는다(중복 회피)."""
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), discord=AutoApproveGate(), state=state
    )
    first, _ = orch.resolve_inputs(None, "")
    second, _ = orch.resolve_inputs(None, "")
    assert first != second


def test_feedback_loop_closes_end_to_end(tmp_path):
    """한 사이클의 분석이 다음 사이클 resolve_inputs의 feedback으로 자동 연결된다."""
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), discord=AutoApproveGate(), state=state
    )
    run = orch.run("강아지 간식")
    analysis = orch.collect_and_analyze(run)
    # 다음 사이클: feedback 인자 없이도 직전 분석이 자동 주입돼야 한다.
    _, next_feedback = orch.resolve_inputs(None, "")
    assert next_feedback == analysis


def test_collect_and_analyze_persists_nonempty_skips_empty(tmp_path, monkeypatch):
    """비어있지 않은 분석은 저장하고, 빈 분석은 기존 피드백을 덮어쓰지 않는다."""
    state = _tmp_state(tmp_path)
    orch = Orchestrator(
        _dry_settings(), telegram=AutoApproveGate(), discord=AutoApproveGate(), state=state
    )
    run = orch.run("주제")

    # 비어있지 않은 분석 → 저장됨.
    monkeypatch.setattr(orch.ai, "analyze_performance", lambda reports: "실제 분석 결과")
    assert orch.collect_and_analyze(run) == "실제 분석 결과"
    assert state.get_feedback() == "실제 분석 결과"

    # 빈 분석(예: 라이브 모드 빈 응답) → 직전 피드백 유지.
    monkeypatch.setattr(orch.ai, "analyze_performance", lambda reports: "")
    assert orch.collect_and_analyze(run) == ""
    assert state.get_feedback() == "실제 분석 결과"


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
