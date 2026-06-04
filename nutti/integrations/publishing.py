"""업로드(4단계) · 성과 수집(5단계): YouTube Data API · Instagram Graph API.

⚠️ 계획서 주의사항: 다계정 운영은 제재 위험 → 초기 1~2계정 파일럿 후 확장.
자동 댓글 링크 금지 → 간식계산기 링크는 설명란/프로필에만 고정.
"""

from __future__ import annotations

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import Metadata, PerformanceReport, UploadResult, VideoAsset

log = get_logger(__name__)


class Publisher:
    """YouTube/Instagram 업로드 및 성과 조회."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def upload_youtube(self, video: VideoAsset, meta: Metadata) -> UploadResult:
        if self.settings.dry_run:
            log.info("dry_run.youtube_upload", script_id=video.script_id, title=meta.title)
            return UploadResult(
                platform="youtube",
                external_id=f"yt_{video.script_id}",
                url=f"https://youtube.com/shorts/{video.script_id}",
            )
        # TODO: YouTube Data API v3 videos.insert (OAuth refresh token 사용)
        raise NotImplementedError("YouTube 업로드 미구현")

    def upload_instagram(self, video: VideoAsset, meta: Metadata) -> UploadResult:
        if self.settings.dry_run:
            log.info("dry_run.instagram_upload", script_id=video.script_id)
            return UploadResult(
                platform="instagram",
                external_id=f"ig_{video.script_id}",
                url=f"https://instagram.com/reel/{video.script_id}",
            )
        # TODO: Instagram Graph API (미디어 컨테이너 생성 → 게시)
        raise NotImplementedError("Instagram 업로드 미구현")

    def fetch_performance(self, upload: UploadResult) -> PerformanceReport:
        if self.settings.dry_run:
            log.info("dry_run.fetch_performance", platform=upload.platform, id=upload.external_id)
            return PerformanceReport(
                platform=upload.platform,
                external_id=upload.external_id,
                views=1234,
                likes=88,
                comments=12,
                avg_view_duration_sec=31.5,
            )
        # TODO: YouTube Analytics API / Instagram Insights 조회
        raise NotImplementedError("성과 수집 미구현")
