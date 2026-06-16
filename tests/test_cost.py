"""편당 제작 비용 집계(estimate_run_cost) 테스트.

영상 길이·백엔드별 단가가 명세와 합계에 정확히 반영되는지, dry_run 표시가
올바른지 검증한다. 외부 호출 없이 도메인 모델만으로 동작한다.
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.models import (
    CostBreakdown,
    Metadata,
    PipelineRun,
    Script,
    VideoAsset,
)
from nutti.pipeline.cost import estimate_run_cost, format_cost
from nutti.pipeline.orchestrator import Orchestrator
from nutti.review.gates import AutoApproveGate


def _run_with_video(duration: float) -> PipelineRun:
    run = PipelineRun(topic="강아지 간식")
    run.script = Script(topic="강아지 간식", body="짧은 대본 본문입니다.")
    run.video = VideoAsset(
        script_id=run.script.id,
        frame_image_path="data/dry_run/frame_x.jpg",
        video_path="data/dry_run/video_x.mp4",
        duration_sec=duration,
    )
    run.metadata = Metadata(title="제목", description="설명", hashtags=["#강아지", "#간식"])
    return run


def test_veo_fast_cost_matches_unit_price():
    """Veo Fast(=$0.10/초) + 프레임 $0.05 + 텍스트 추정의 합계가 맞아야 한다."""
    settings = Settings(NUTTI_DRY_RUN=True, NUTTI_VIDEO_BACKEND="veo")
    cost = estimate_run_cost(_run_with_video(32.0), settings)

    labels = {item.label for item in cost.items}
    assert "시작 프레임 (나노바나나)" in labels
    assert "영상 생성 (Veo Fast)" in labels

    video_item = next(i for i in cost.items if i.label.startswith("영상 생성"))
    assert video_item.usd == 0.10 * 32.0  # 정확히 초당 단가 × 길이
    # 합계 = 프레임(0.05) + 영상(3.20) + 텍스트 추정(작은 양수)
    assert cost.total_usd > 0.05 + 3.20
    assert cost.dry_run is True


def test_kling_standard_unit_price():
    """Kling 백엔드는 $0.084/초로 계산된다."""
    settings = Settings(NUTTI_DRY_RUN=True, NUTTI_VIDEO_BACKEND="kling")
    cost = estimate_run_cost(_run_with_video(10.0), settings)
    video_item = next(i for i in cost.items if i.label.startswith("영상 생성"))
    assert "Kling Standard" in video_item.label
    assert abs(video_item.usd - 0.084 * 10.0) < 1e-9


def test_kling_pro_unit_price():
    """Kling Pro 경로(모델 ID에 'pro' 포함)는 $0.112/초로 계산된다."""
    settings = Settings(
        NUTTI_DRY_RUN=True,
        NUTTI_VIDEO_BACKEND="kling",
        NUTTI_KLING_MODEL="fal-ai/kling-video/v2.1/pro/image-to-video",
    )
    cost = estimate_run_cost(_run_with_video(10.0), settings)
    video_item = next(i for i in cost.items if i.label.startswith("영상 생성"))
    assert "Kling Pro" in video_item.label
    assert abs(video_item.usd - 0.112 * 10.0) < 1e-9


def test_unknown_video_model_falls_back_and_marks_estimate():
    """단가표에 없는 모델은 조용히 틀리지 않도록 보수적 기본값 + '단가추정' 라벨."""
    settings = Settings(
        NUTTI_DRY_RUN=True,
        NUTTI_VIDEO_BACKEND="veo",
        NUTTI_VEO_MODEL="veo-9.9-unknown-preview",
    )
    cost = estimate_run_cost(_run_with_video(8.0), settings)
    video_item = next(i for i in cost.items if i.label.startswith("영상 생성"))
    assert "단가추정" in video_item.label
    assert abs(video_item.usd - 0.40 * 8.0) < 1e-9  # 보수적 standard 단가


def test_veo_standard_when_not_fast():
    """fast/lite가 아닌 veo 모델은 standard 단가($0.40/초)로 떨어진다."""
    settings = Settings(
        NUTTI_DRY_RUN=True,
        NUTTI_VIDEO_BACKEND="veo",
        NUTTI_VEO_MODEL="veo-3.1-generate-preview",
    )
    cost = estimate_run_cost(_run_with_video(8.0), settings)
    video_item = next(i for i in cost.items if i.label.startswith("영상 생성"))
    assert "Veo Standard" in video_item.label
    assert abs(video_item.usd - 0.40 * 8.0) < 1e-9


def test_text_line_is_marked_estimated():
    """텍스트 생성 라인은 추정치(estimated=True)로 표시된다."""
    settings = Settings(NUTTI_DRY_RUN=True, NUTTI_VIDEO_BACKEND="veo")
    cost = estimate_run_cost(_run_with_video(8.0), settings)
    text_item = next(i for i in cost.items if i.label.startswith("텍스트"))
    assert text_item.estimated is True


def test_no_video_no_frame_or_video_lines():
    """영상이 없으면 프레임·영상 라인이 빠지고 텍스트만 남는다."""
    settings = Settings(NUTTI_DRY_RUN=True)
    run = PipelineRun(topic="주제")
    run.script = Script(topic="주제", body="본문")
    cost = estimate_run_cost(run, settings)
    labels = [i.label for i in cost.items]
    assert not any(label.startswith("영상") for label in labels)
    assert not any(label.startswith("시작 프레임") for label in labels)


def test_total_is_sum_of_items():
    settings = Settings(NUTTI_DRY_RUN=True, NUTTI_VIDEO_BACKEND="veo")
    cost = estimate_run_cost(_run_with_video(24.0), settings)
    # total_usd는 4자리 반올림이므로 원합과 반올림 오차 내에서 일치해야 한다.
    assert cost.total_usd == round(sum(i.usd for i in cost.items), 4)


def test_format_cost_dry_run_shows_zero_spend():
    settings = Settings(NUTTI_DRY_RUN=True, NUTTI_VIDEO_BACKEND="veo")
    cost = estimate_run_cost(_run_with_video(8.0), settings)
    text = format_cost(cost)
    assert "DRY_RUN" in text
    assert "실제 지출: $0" in text


def test_format_cost_live_shows_total():
    cost = CostBreakdown(items=[], total_usd=3.25, dry_run=False)
    text = format_cost(cost)
    assert "합계: $3.25" in text
    assert "DRY_RUN" not in text


def test_orchestrator_attaches_cost_to_run():
    """파이프라인을 끝까지 돌리면 run.cost가 채워진다(배선 확인)."""
    settings = Settings(NUTTI_DRY_RUN=True, NUTTI_ENV="test", NUTTI_VIDEO_BACKEND="veo")
    orch = Orchestrator(settings, telegram=AutoApproveGate(), discord=AutoApproveGate())
    run = orch.run("강아지 닭가슴살 간식 적정량")
    assert run.cost is not None
    assert run.cost.total_usd > 0
    assert run.cost.dry_run is True
    # 영상 라인이 dry_run 실측 길이로 채워졌는지(8초 클립 × 비트) — veo 단가 적용.
    video_item = next(i for i in run.cost.items if i.label.startswith("영상 생성"))
    assert "Veo Fast" in video_item.label


def test_orchestrator_cost_wired_for_kling_backend():
    """Kling 백엔드 오케스트레이터 경로에서도 cost가 Kling 단가로 집계된다."""
    settings = Settings(NUTTI_DRY_RUN=True, NUTTI_ENV="test", NUTTI_VIDEO_BACKEND="kling")
    orch = Orchestrator(settings, telegram=AutoApproveGate(), discord=AutoApproveGate())
    run = orch.run("강아지 간식")
    assert run.cost is not None and run.cost.total_usd > 0
    video_item = next(i for i in run.cost.items if i.label.startswith("영상 생성"))
    assert "Kling" in video_item.label
