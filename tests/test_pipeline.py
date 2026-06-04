"""dry_run 파이프라인 스모크 테스트.

외부 API 키 없이 전 단계가 끝까지 돌고, 검수 게이트가 동작하는지 검증한다.
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.models import ContentFormat, ReviewDecision, ReviewRequest, Stage
from nutti.pipeline.orchestrator import GateRejected, Orchestrator
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
