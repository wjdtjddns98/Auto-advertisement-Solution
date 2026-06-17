"""파이프라인 전반에서 공유하는 도메인 모델."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def _new_id() -> str:
    return uuid4().hex[:12]


def _utcnow() -> datetime:
    """timezone-aware UTC now (deprecated datetime.utcnow() 회피)."""
    return datetime.now(timezone.utc)


class ContentFormat(str, Enum):
    SHORTS = "youtube_shorts"
    REELS = "instagram_reels"


class Stage(str, Enum):
    """파이프라인 단계 식별자."""

    SCRIPT = "script"          # 1단계
    VIDEO = "video"            # 2단계
    METADATA = "metadata"      # 3단계
    UPLOAD = "upload"          # 4단계
    ANALYTICS = "analytics"    # 5단계


class ReviewDecision(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISE = "revise"


class Script(BaseModel):
    """1단계 산출물: 약 35초 쇼츠/릴스 대본(비트별 독립 클립을 ffmpeg로 스티칭)."""

    id: str = Field(default_factory=_new_id)
    topic: str
    body: str
    prompt: str = ""
    fact_checked: bool = False
    # 영상 비트별 대사(훅 → 핵심설명 → 팁 → 마무리/CTA, 4비트). 각 비트가 하나의
    # 클립이 된다(veo: 8초 고정, kling: 내레이션 길이에 맞춘 5/10초).
    # 비면 body 전체를 단일 클립 대사로 쓴다(하위호환·단일컷 폴백).
    beats: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


class VideoAsset(BaseModel):
    """2단계 산출물: Veo 단일컷 영상과 중간 자산."""

    script_id: str
    frame_image_path: str | None = None  # FLUX Kontext(마스코트 시작 프레임) 로컬 경로
    video_path: str | None = None        # FalVeoClient가 즉시 다운로드해 저장한 로컬 경로
    final_url: str | None = None         # 최종 산출물 위치(로컬 파일 경로 문자열 허용)
    preview_url: str | None = None
    duration_sec: float = Field(default=8.0)  # Veo 단일컷 기본 8초


class Metadata(BaseModel):
    """3단계 산출물: 업로드용 메타데이터."""

    title: str
    description: str
    hashtags: list[str] = Field(default_factory=list)


class UploadResult(BaseModel):
    """4단계 산출물: 플랫폼별 업로드 결과."""

    platform: str                  # "youtube" | "instagram"
    external_id: str
    url: str
    uploaded_at: datetime = Field(default_factory=_utcnow)


class PerformanceReport(BaseModel):
    """5단계 산출물: 콘텐츠별 성과 지표."""

    platform: str
    external_id: str
    views: int = 0
    likes: int = 0
    comments: int = 0
    avg_view_duration_sec: float = 0.0
    collected_at: datetime = Field(default_factory=_utcnow)


class CostLineItem(BaseModel):
    """비용 명세의 한 줄(항목별 단가·수량·소계)."""

    label: str                     # 예: "영상 생성 (Veo Fast)"
    detail: str = ""               # 예: "$0.100/초 × 32.0초"
    usd: float = 0.0               # 소계(USD)
    estimated: bool = False        # True면 실측이 아닌 추정치(예: 텍스트 토큰)


class CostBreakdown(BaseModel):
    """한 편 제작에 든 비용 명세와 합계.

    dry_run 실행이면 실제 지출은 0이고 total_usd는 "라이브였다면" 예상 비용이다.
    """

    items: list[CostLineItem] = Field(default_factory=list)
    total_usd: float = 0.0
    dry_run: bool = True           # True면 total_usd는 실제 지출이 아닌 예상치


class ReviewRequest(BaseModel):
    """검수 게이트로 보내는 승인 요청."""

    id: str = Field(default_factory=_new_id)
    stage: Stage
    title: str
    preview: str                   # 텍스트 미리보기 또는 미리보기 URL
    decision: ReviewDecision = ReviewDecision.PENDING
    note: str = ""
    message_id: int | None = None  # 텔레그램 검수 메시지 ID(버튼 수정/콜백 매칭용)
    revised_content: str | None = None  # 사용자가 입력한 수정 대본 내용
    media_path: str | None = None       # 영상 파일 전송용 로컬 경로


class PipelineRun(BaseModel):
    """한 편의 콘텐츠가 파이프라인을 통과하는 전체 상태."""

    id: str = Field(default_factory=_new_id)
    topic: str
    content_format: ContentFormat = ContentFormat.SHORTS
    current_stage: Stage = Stage.SCRIPT
    script: Script | None = None
    video: VideoAsset | None = None
    metadata: Metadata | None = None
    uploads: list[UploadResult] = Field(default_factory=list)
    reports: list[PerformanceReport] = Field(default_factory=list)
    # 한 사이클 종료 시 집계한 제작 비용 명세(영상·프레임·텍스트). 미집계 시 None.
    cost: CostBreakdown | None = None
    created_at: datetime = Field(default_factory=_utcnow)
