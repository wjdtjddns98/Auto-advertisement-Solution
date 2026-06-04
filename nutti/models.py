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
    """1단계 산출물: 60초 내외 쇼츠/릴스 대본."""

    id: str = Field(default_factory=_new_id)
    topic: str
    body: str
    prompt: str = ""
    fact_checked: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class VideoAsset(BaseModel):
    """2단계 산출물: 합성된 최종 영상과 중간 자산."""

    script_id: str
    character_clip_url: str | None = None     # Hedra Character-3
    scene_clip_urls: list[str] = Field(default_factory=list)  # Seedance/Kling
    subtitle_url: str | None = None           # AssemblyAI
    final_url: str | None = None
    preview_url: str | None = None
    duration_sec: float = 0.0


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


class ReviewRequest(BaseModel):
    """검수 게이트로 보내는 승인 요청."""

    id: str = Field(default_factory=_new_id)
    stage: Stage
    title: str
    preview: str                   # 텍스트 미리보기 또는 미리보기 URL
    decision: ReviewDecision = ReviewDecision.PENDING
    note: str = ""


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
    created_at: datetime = Field(default_factory=_utcnow)
