"""한 사이클 제작 비용 집계.

파이프라인이 끝난 뒤 `PipelineRun` 산출물(영상 길이·프레임·생성 텍스트)을 보고
편당 비용을 명세로 집계한다. 단가 기준은 `docs/cost-analysis.md`.

- **영상·시작 프레임**은 실측에 가깝다(영상 길이초 `duration_sec`는 파이프라인이
  정확히 계산하고, 시작 프레임은 항상 1장).
- **텍스트 생성**(대본·팩트체크·메타데이터)은 토큰 사용량을 별도로 받아오지 않으므로
  생성된 글자 수로 추정한다(`estimated=True`로 표시). 금액은 보통 수 센트 이하다.

dry_run 실행이면 실제 지출은 0이지만, "라이브였다면" 들었을 예상 비용을 같은 방식으로
계산해 보여준다(`CostBreakdown.dry_run=True`).
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import CostBreakdown, CostLineItem, PipelineRun

log = get_logger(__name__)

# 시작 프레임 1장(나노바나나 / Gemini 2.5 Flash Image) 단가(USD).
_FRAME_USD = 0.05

# 텍스트 생성 추정 단가(USD / 1K 토큰, output 기준 블렌디드). Gemini 2.5 Flash 근사치.
# 한국어는 대략 글자당 ~1토큰으로 보수적으로 잡는다. 금액 자체가 작아 정밀도는 부차적.
_TEXT_USD_PER_1K_TOKENS = 0.0025


def _video_unit_price(settings: Settings) -> tuple[float, str]:
    """영상 백엔드·모델에 따른 초당 단가(USD)와 표시용 모델명을 돌려준다.

    단가표는 docs/cost-analysis.md 기준. 단가가 표에 없는 신규/미확정 모델이 오면
    조용히 틀린 값을 쓰지 않도록 경고 로그를 남기고 보수적 기본값으로 떨어진다
    (veo=standard $0.40, kling=standard $0.084). 라벨에 "(단가추정)"을 붙여 표시한다.
    """
    if settings.video_backend == "kling":
        model = settings.kling_model.lower()
        if "pro" in model:
            return 0.112, "Kling Pro"
        if "standard" in model:
            return 0.084, "Kling Standard"
        log.warning("cost.unknown_video_model", backend="kling", model=settings.kling_model)
        return 0.084, "Kling(단가추정)"
    # 기본 veo 경로.
    model = settings.veo_model.lower()
    if "fast" in model:
        return 0.10, "Veo Fast"
    if "lite" in model:
        return 0.05, "Veo Lite"
    if "standard" in model or "generate" in model:
        return 0.40, "Veo Standard"
    log.warning("cost.unknown_video_model", backend="veo", model=settings.veo_model)
    return 0.40, "Veo(단가추정)"


def _text_output_chars(run: PipelineRun) -> int:
    """이번 사이클에서 LLM이 생성한 텍스트 글자 수 합계(추정용 프록시)."""
    chars = 0
    if run.script is not None:
        chars += len(run.script.body)
    if run.metadata is not None:
        meta = run.metadata
        chars += len(meta.title) + len(meta.description)
        chars += sum(len(tag) for tag in meta.hashtags)
    return chars


def estimate_run_cost(run: PipelineRun, settings: Settings) -> CostBreakdown:
    """한 사이클의 제작 비용 명세를 집계한다.

    영상·프레임은 산출물 기준(거의 실측), 텍스트는 글자 수 기반 추정이다.
    dry_run이면 total_usd는 실제 지출이 아닌 예상치다(`dry_run=True`).
    """
    items: list[CostLineItem] = []

    # 1) 시작 프레임(나노바나나) — 영상이 만들어졌으면 1장.
    if run.video is not None and run.video.frame_image_path:
        items.append(
            CostLineItem(
                label="시작 프레임 (나노바나나)",
                detail=f"${_FRAME_USD:.3f}/장 × 1장",
                usd=_FRAME_USD,
            )
        )

    # 2) 영상 생성 — duration_sec(파이프라인 실측)에 모델별 초당 단가를 곱한다.
    if run.video is not None:
        per_sec, model_name = _video_unit_price(settings)
        seconds = float(run.video.duration_sec)
        items.append(
            CostLineItem(
                label=f"영상 생성 ({model_name})",
                detail=f"${per_sec:.3f}/초 × {seconds:.1f}초",
                usd=per_sec * seconds,
            )
        )

    # 3) 텍스트 생성(대본·팩트체크·메타데이터) — 토큰 미수집이라 글자 수로 추정.
    text_chars = _text_output_chars(run)
    if text_chars:
        est_tokens = float(text_chars)  # 한국어 보수적 가정: 글자당 ~1토큰
        text_usd = est_tokens / 1000.0 * _TEXT_USD_PER_1K_TOKENS
        items.append(
            CostLineItem(
                label="텍스트 생성 (대본·팩트체크·메타데이터)",
                detail=f"~{int(est_tokens)}토큰 추정 × ${_TEXT_USD_PER_1K_TOKENS:.4f}/1K",
                usd=text_usd,
                estimated=True,
            )
        )

    total = round(sum(item.usd for item in items), 4)
    return CostBreakdown(items=items, total_usd=total, dry_run=settings.dry_run)


def format_cost(cost: CostBreakdown) -> str:
    """비용 명세를 사람이 읽는 한국어 요약 문자열로 만든다(CLI 출력용)."""
    header = "[제작 비용 — 예상(DRY_RUN)]" if cost.dry_run else "[제작 비용]"
    lines = [header]
    for item in cost.items:
        mark = " (추정)" if item.estimated else ""
        detail = f" ({item.detail})" if item.detail else ""
        lines.append(f"  - {item.label}{mark}: ${item.usd:.4f}{detail}")
    if cost.dry_run:
        lines.append(f"  합계(라이브 예상): ${cost.total_usd:.4f}  ·  실제 지출: $0 (DRY_RUN)")
    else:
        lines.append(f"  합계: ${cost.total_usd:.4f}")
    return "\n".join(lines)
