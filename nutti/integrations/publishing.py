"""업로드(4단계) · 성과 수집(5단계): YouTube Data API · Instagram Graph API.

⚠️ 계획서 주의사항: 다계정 운영은 제재 위험 → 초기 1~2계정 파일럿 후 확장.
자동 댓글 링크 금지 → 간식계산기 링크는 설명란/프로필에만 고정.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import Metadata, PerformanceReport, UploadResult, VideoAsset

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

# Instagram 컨테이너 폴링 설정 상수
INSTAGRAM_POLL_TIMEOUT_SEC = 600.0
INSTAGRAM_POLL_INTERVAL_SEC = 5.0

# Instagram Graph API 버전
_IG_BASE = "https://graph.facebook.com/v25.0"

# YouTube OAuth2 토큰 교환 엔드포인트
_YT_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Instagram Reels 인사이트 지표 (v22.0 이후 plays/video_views 삭제됨 → views 사용)
_IG_REELS_METRICS = [
    "views",
    "reach",
    "likes",
    "comments",
    "shares",
    "saved",
    "total_interactions",
    "ig_reels_avg_watch_time",  # ms 단위
]


# ---------------------------------------------------------------------------
# 예외 클래스
# ---------------------------------------------------------------------------


class PublishError(RuntimeError):
    """업로드/성과 수집 실패(영구 오류). HTTP 4xx·5xx 등에 사용한다."""


class PublishTimeoutError(PublishError):
    """폴링 제한 시간 안에 작업이 완료되지 않은 경우의 타임아웃."""


# ---------------------------------------------------------------------------
# 유틸 함수
# ---------------------------------------------------------------------------


def _usable_key(value: str | None) -> bool:
    """API 키 값이 실제로 쓸 수 있는지(비어 있지 않고 주석이 아님) 판정한다.

    pydantic-settings는 `.env`의 인라인 주석을 분리하지 않으므로,
    `KEY=   # 설명`처럼 빈 값 뒤에 주석이 붙으면 키 값이 `'# 설명'`이라는
    truthy 문자열로 파싱된다. 단순 truthiness 검사는 이런 더미 값을 진짜 키로
    오인해 fast-fail 가드를 우회시키므로, strip 후 주석(`#` 시작)을 배제한다.
    """
    if not value:
        return False
    stripped = value.strip()
    return bool(stripped) and not stripped.startswith("#")


# ---------------------------------------------------------------------------
# YouTube 클라이언트
# ---------------------------------------------------------------------------


class YouTubeClient:
    """YouTube OAuth2 토큰 교환 + 업로드 클라이언트(httpx 기반).

    httpx는 실 경로(non-dry_run)에서만 lazy import한다.
    `http` 주입 시 네트워크 없이 테스트할 수 있다.
    close()/컨텍스트 매니저로 httpx 커넥션 풀을 명시적으로 닫아야 한다.
    Publisher가 내부 생성한 인스턴스는 try/finally로 반드시 close()를 호출한다.
    """

    def __init__(self, settings: Settings, *, http=None):
        self.settings = settings
        self._http = http

    @property
    def http(self):
        """httpx.Client를 지연 생성한다(주입이 없을 때만, 실 경로 전용)."""
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=httpx.Timeout(30.0))
        return self._http

    def close(self) -> None:
        """httpx 커넥션 풀을 닫는다. 멱등 — 이미 닫혔거나 http가 None이면 no-op."""
        if self._http is not None:
            self._http.close()
            self._http = None  # close 후 재생성 누수 방지

    def __enter__(self):
        return self

    def __exit__(self, *_) -> None:
        self.close()

    @staticmethod
    def _raise_for_publish(resp, what: str) -> None:
        """HTTP 4xx·5xx를 PublishError로 전파한다(상태 코드만 노출).

        status_code 속성이 없는 응답은 방어적으로 PublishError를 던진다.
        """
        code = getattr(resp, "status_code", None)
        if not isinstance(code, int):
            raise PublishError(f"{what} 응답에 유효한 status_code가 없습니다")
        if code >= 400:
            raise PublishError(f"{what} HTTP {code}")

    def exchange_token(self) -> str:
        """OAuth2 refresh_token으로 access_token을 교환한다.

        POST https://oauth2.googleapis.com/token
        응답 필드: access_token(str), expires_in(int, 초 단위)
        에러 응답: HTTP 400/401 + {"error": "...", "error_description": "..."}
        """
        import httpx  # lazy import — dry_run 경로에서는 불필요

        try:
            resp = self.http.post(
                _YT_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.settings.youtube_refresh_token,
                    "client_id": self.settings.youtube_client_id,
                    "client_secret": self.settings.youtube_client_secret,
                },
            )
        except (httpx.TransportError, httpx.TooManyRedirects):
            # 전송 계층 오류 — 요청 바디(client_secret/refresh_token 포함)를 노출하지 않는다.
            raise PublishError("YouTube OAuth 토큰 교환 전송 오류") from None
        self._raise_for_publish(resp, "YouTube OAuth 토큰 교환")
        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            # 응답 키 목록만 노출(값에 토큰 포함 가능)
            raise PublishError(
                f"YouTube OAuth 응답에 access_token이 없습니다 (응답 키: {list(data.keys())})"
            )
        return str(access_token)

    def upload_video(self, video: VideoAsset, meta: Metadata, access_token: str) -> str:
        """YouTube Data API v3 videos.insert(resumable)로 영상을 업로드하고 video_id를 반환한다.

        resumable upload 2단계:
        (1) Initiation POST — 메타데이터 JSON으로 세션 URI를 발급받는다.
            POST https://www.googleapis.com/upload/youtube/v3/videos
              ?uploadType=resumable&part=snippet,status
            Location 헤더에 세션 URI 반환.
        (2) Upload PUT — 세션 URI에 실제 영상 바이트를 단일 청크로 전송한다.
            바이트 소스는 `_load_video_bytes`가 결정한다(로컬 파일 또는 원격 URL).
            쇼츠/릴스는 용량이 작아 단일 PUT으로 충분하다(청크 분할 불요).
        최종 응답(HTTP 200/201) 최상위 `id` 필드가 video_id다.
        """
        import httpx  # lazy import — dry_run 경로에서는 불필요

        body = {
            "snippet": {
                "title": meta.title,
                "description": meta.description,
                "tags": meta.hashtags,
                "categoryId": "22",  # People & Blogs
            },
            "status": {"privacyStatus": "public"},
        }
        # 1) Initiation POST — 세션 URI 발급
        try:
            resp = self.http.post(
                "https://www.googleapis.com/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                json=body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": "video/*",
                },
            )
        except (httpx.TransportError, httpx.TooManyRedirects):
            # 전송 계층 오류 — Authorization 헤더(access_token)를 노출하지 않는다.
            raise PublishError("YouTube 업로드 initiation 전송 오류") from None
        self._raise_for_publish(resp, "YouTube 업로드 initiation")
        # 세션 URI는 Location 헤더에 반환됨
        location = resp.headers.get("Location") or resp.headers.get("location", "")
        if not location:
            raise PublishError("YouTube 업로드 initiation 응답에 Location 헤더가 없습니다")

        # 2) 영상 바이트 확보 후 세션 URI로 단일 PUT 전송
        data = self._load_video_bytes(video)
        try:
            put_resp = self.http.put(
                location,
                content=data,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "video/*",
                    "Content-Length": str(len(data)),
                },
            )
        except (httpx.TransportError, httpx.TooManyRedirects):
            # 전송 계층 오류 — 세션 URI(업로드 토큰 포함 가능)를 노출하지 않는다.
            raise PublishError("YouTube 영상 업로드(PUT) 전송 오류") from None
        # 308 Resume Incomplete: 서버가 바이트를 전부 받지 못한 상태(부분 업로드).
        # 단일 청크 PUT만 지원하므로 재개 로직이 없다 → 명확한 미완료 오류로 분리.
        # (2xx 미만이라 _raise_for_publish는 308을 통과시켜 id 누락 오류로 오분류된다.)
        if getattr(put_resp, "status_code", None) == 308:
            raise PublishError("YouTube 업로드 미완료 — 308 Resume Incomplete (단일 PUT 재개 미지원)")
        self._raise_for_publish(put_resp, "YouTube 영상 업로드")
        payload = put_resp.json()
        video_id = payload.get("id")
        if not video_id:
            raise PublishError(
                f"YouTube 업로드 응답에 id가 없습니다 (응답 키: {list(payload.keys())})"
            )
        return str(video_id)

    def _load_video_bytes(self, video: VideoAsset) -> bytes:
        """업로드할 영상 바이트를 확보한다.

        소스 우선순위: `video_path`(FalVeoClient가 다운로드한 로컬 경로) → `final_url`.
        - 로컬 파일 경로면 그대로 읽는다(현재 파이프라인 기본 — final_url=video_path).
        - http(s) URL이면 다운로드한다(원격 산출물 대비).
        에러 메시지에는 파일명만 노출하고 전체 경로/URL은 가린다(redaction).
        """
        import httpx  # lazy import — dry_run 경로에서는 불필요

        source = video.video_path or video.final_url
        if not source:
            raise PublishError(
                "업로드할 영상 위치가 없습니다 (video_path·final_url 모두 비어 있음)"
            )
        if source.startswith(("http://", "https://")):
            # 공개 URL 전용 경로 — Authorization 헤더 없이 GET 한다(현재 파이프라인 미도달:
            # final_url=video_path 로컬 경로). 인증 필요 URL(만료 presigned 등)을 쓰려면
            # 인증 전략을 별도로 정해야 한다.
            try:
                resp = self.http.get(source)
            except (httpx.TransportError, httpx.TooManyRedirects):
                raise PublishError("YouTube 업로드용 영상 다운로드 전송 오류") from None
            self._raise_for_publish(resp, "YouTube 업로드용 영상 다운로드")
            return resp.content
        path = Path(source)
        if not path.is_file():
            raise PublishError(f"업로드할 영상 파일을 찾을 수 없습니다: {path.name}")
        return path.read_bytes()

    def fetch_analytics(self, external_id: str) -> dict:
        """YouTube Analytics API로 영상 성과 지표를 조회한다.

        TODO(live): YouTube Analytics API v2 엔드포인트 확정 필요.
        - GET https://youtubeanalytics.googleapis.com/v2/reports
          ?ids=channel==MINE&startDate=...&endDate=...
          &metrics=views,likes,comments,averageViewDuration
          &filters=video=={external_id}
        현재는 NotImplementedError 없이 빈 dict를 반환(fetch_performance에서 0으로 정규화됨).
        """
        # TODO(live): 실제 YouTube Analytics API v2 호출 구현 필요
        log.warning("youtube.fetch_analytics.not_implemented", video_id=external_id)
        return {}


# ---------------------------------------------------------------------------
# Instagram 클라이언트
# ---------------------------------------------------------------------------


class InstagramClient:
    """Instagram Graph API 클라이언트(httpx 기반).

    Reels 업로드 3단계 플로우: 컨테이너 생성 → 상태 폴링 → 게시.
    httpx는 실 경로에서만 lazy import한다. `http` 주입 시 네트워크 없이 테스트.
    close()/컨텍스트 매니저로 httpx 커넥션 풀을 명시적으로 닫아야 한다.
    Publisher가 내부 생성한 인스턴스는 try/finally로 반드시 close()를 호출한다.

    주의: 폴링 루프(clock/sleep)는 InstagramClient가 아닌 Publisher가 소유한다.
    """

    def __init__(self, settings: Settings, *, http=None):
        self.settings = settings
        self._http = http

    @property
    def http(self):
        """httpx.Client를 지연 생성한다(주입이 없을 때만, 실 경로 전용)."""
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=httpx.Timeout(30.0))
        return self._http

    def close(self) -> None:
        """httpx 커넥션 풀을 닫는다. 멱등 — 이미 닫혔거나 http가 None이면 no-op."""
        if self._http is not None:
            self._http.close()
            self._http = None  # close 후 재생성 누수 방지

    def __enter__(self):
        return self

    def __exit__(self, *_) -> None:
        self.close()

    @staticmethod
    def _raise_for_publish(resp, what: str) -> None:
        """HTTP 4xx·5xx를 PublishError로 전파한다(상태 코드만 노출)."""
        code = getattr(resp, "status_code", None)
        if not isinstance(code, int):
            raise PublishError(f"{what} 응답에 유효한 status_code가 없습니다")
        if code >= 400:
            raise PublishError(f"{what} HTTP {code}")

    def create_container(self, video: VideoAsset, meta: Metadata) -> str:
        """Reels 미디어 컨테이너를 생성하고 creation_id를 반환한다.

        TODO(live): video_url은 Meta 서버가 직접 cURL로 다운로드할 수 있는
        공개 URL이어야 한다. VideoAsset.final_url이 로컬 경로이거나 인증이
        필요한 URL이면 컨테이너 생성이 실패한다.
        """
        import httpx  # lazy import — dry_run 경로에서는 불필요

        account_id = self.settings.instagram_account_id
        access_token = self.settings.instagram_access_token
        caption_parts = [meta.description]
        if meta.hashtags:
            caption_parts.append(" ".join(f"#{tag}" for tag in meta.hashtags))
        caption = "\n".join(caption_parts)

        try:
            resp = self.http.post(
                f"{_IG_BASE}/{account_id}/media",
                data={
                    "media_type": "REELS",
                    "video_url": video.final_url,  # TODO(live): 반드시 공개 URL
                    "caption": caption,
                    "access_token": access_token,
                },
            )
        except (httpx.TransportError, httpx.TooManyRedirects):
            # 전송 계층 오류 — 요청 바디(access_token 포함)를 노출하지 않는다.
            raise PublishError("Instagram 컨테이너 생성 전송 오류") from None
        self._raise_for_publish(resp, "Instagram 컨테이너 생성")
        data = resp.json()
        creation_id = data.get("id")
        if not creation_id:
            raise PublishError(
                f"Instagram 컨테이너 응답에 id가 없습니다 (응답 키: {list(data.keys())})"
            )
        return str(creation_id)

    def poll_container(self, creation_id: str) -> str:
        """컨테이너 처리 상태를 조회하고 status_code 문자열을 반환한다.

        반환 가능한 값: FINISHED / IN_PROGRESS / ERROR / EXPIRED / PUBLISHED
        """
        import httpx  # lazy import — dry_run 경로에서는 불필요

        access_token = self.settings.instagram_access_token
        url = f"{_IG_BASE}/{creation_id}"
        try:
            resp = self.http.get(
                url,
                params={
                    "fields": "status_code",
                    "access_token": access_token,
                },
            )
        except (httpx.TransportError, httpx.TooManyRedirects):
            # from None 으로 원본 예외(access_token이 포함된 전체 URL)를 억제한다.
            raise PublishError("Instagram 컨테이너 상태 조회 전송 오류") from None
        self._raise_for_publish(resp, "Instagram 컨테이너 상태 조회")
        data = resp.json()
        return str(data.get("status_code", ""))

    def publish(self, creation_id: str) -> str:
        """컨테이너를 게시하고 media_id를 반환한다."""
        import httpx  # lazy import — dry_run 경로에서는 불필요

        account_id = self.settings.instagram_account_id
        access_token = self.settings.instagram_access_token
        try:
            resp = self.http.post(
                f"{_IG_BASE}/{account_id}/media_publish",
                data={
                    "creation_id": creation_id,
                    "access_token": access_token,
                },
            )
        except (httpx.TransportError, httpx.TooManyRedirects):
            # 전송 계층 오류 — 요청 바디(access_token 포함)를 노출하지 않는다.
            raise PublishError("Instagram 게시 전송 오류") from None
        self._raise_for_publish(resp, "Instagram 게시")
        data = resp.json()
        media_id = data.get("id")
        if not media_id:
            raise PublishError(
                f"Instagram 게시 응답에 id가 없습니다 (응답 키: {list(data.keys())})"
            )
        return str(media_id)

    def fetch_permalink(self, media_id: str) -> str:
        """게시된 Reel의 permalink URL을 조회한다."""
        import httpx  # lazy import — dry_run 경로에서는 불필요

        access_token = self.settings.instagram_access_token
        url = f"{_IG_BASE}/{media_id}"
        try:
            resp = self.http.get(
                url,
                params={
                    "fields": "permalink",
                    "access_token": access_token,
                },
            )
        except (httpx.TransportError, httpx.TooManyRedirects):
            # from None 으로 원본 예외(access_token이 포함된 전체 URL)를 억제한다.
            raise PublishError("Instagram permalink 조회 전송 오류") from None
        self._raise_for_publish(resp, "Instagram permalink 조회")
        data = resp.json()
        permalink = data.get("permalink", "")
        return str(permalink)

    def fetch_insights(self, media_id: str) -> dict:
        """Reels 미디어 인사이트 지표를 조회한다.

        TODO(live): 게시 직후에는 지표가 채워지지 않으므로, 수십 분 후에 조회하는
        것을 권장한다. 빈/null 지표는 0으로 정규화해 반환한다.

        주의: v22.0 이후 plays/video_views는 deprecated → views를 사용한다.
        ig_reels_avg_watch_time은 밀리초(ms) 단위다.
        """
        import httpx  # lazy import — dry_run 경로에서는 불필요

        access_token = self.settings.instagram_access_token
        url = f"{_IG_BASE}/{media_id}/insights"
        try:
            resp = self.http.get(
                url,
                params={
                    "metric": ",".join(_IG_REELS_METRICS),
                    "access_token": access_token,
                },
            )
        except (httpx.TransportError, httpx.TooManyRedirects):
            # from None 으로 원본 예외(access_token이 포함된 전체 URL)를 억제한다.
            raise PublishError("Instagram 인사이트 조회 전송 오류") from None
        self._raise_for_publish(resp, "Instagram 인사이트 조회")
        raw = resp.json()
        # 응답 구조: {"data": [{"name": "views", "values": [{"value": 100}]}, ...]}
        result: dict = {}
        for item in raw.get("data", []):
            name = item.get("name", "")
            values = item.get("values") or []
            value = values[0].get("value", 0) if values else 0
            result[name] = value
        return result


# ---------------------------------------------------------------------------
# Publisher 파사드
# ---------------------------------------------------------------------------


class Publisher:
    """YouTube/Instagram 업로드 및 성과 조회."""

    def __init__(
        self,
        settings: Settings,
        *,
        yt_client=None,
        ig_client=None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.settings = settings
        self._yt_client = yt_client
        self._ig_client = ig_client
        self._clock: Callable[[], float] = clock or time.monotonic
        self._sleep: Callable[[float], None] = sleep or time.sleep

    def upload_youtube(self, video: VideoAsset, meta: Metadata) -> UploadResult:
        """YouTube Shorts에 영상을 업로드하고 UploadResult를 반환한다."""
        if self.settings.dry_run:
            log.info("dry_run.youtube_upload", script_id=video.script_id, title=meta.title)
            return UploadResult(
                platform="youtube",
                external_id=f"yt_{video.script_id}",
                url=f"https://youtube.com/shorts/{video.script_id}",
            )

        # 필수 OAuth 키 검증 (주입 클라이언트가 없을 때만)
        if self._yt_client is None:
            if not _usable_key(self.settings.youtube_client_id):
                raise ValueError("YOUTUBE_CLIENT_ID가 비어 있습니다 — dry_run=False 시 필수입니다.")
            if not _usable_key(self.settings.youtube_client_secret):
                raise ValueError(
                    "YOUTUBE_CLIENT_SECRET이 비어 있습니다 — dry_run=False 시 필수입니다."
                )
            if not _usable_key(self.settings.youtube_refresh_token):
                raise ValueError(
                    "YOUTUBE_REFRESH_TOKEN이 비어 있습니다 — dry_run=False 시 필수입니다."
                )

        # 주입 클라이언트: 호출자가 수명 관리 → close 금지.
        # 내부 생성 클라이언트: Publisher가 소유 → try/finally로 반드시 close.
        _own_yt = self._yt_client is None
        client = self._yt_client or YouTubeClient(self.settings)
        try:
            # 1) OAuth access_token 교환
            access_token = client.exchange_token()
            # 2) 영상 업로드 → video_id
            video_id = client.upload_video(video, meta, access_token)
        finally:
            if _own_yt:
                client.close()
        log.info("youtube.upload.done", script_id=video.script_id, video_id=video_id)
        return UploadResult(
            platform="youtube",
            external_id=video_id,
            url=f"https://youtube.com/shorts/{video_id}",
        )

    def upload_instagram(self, video: VideoAsset, meta: Metadata) -> UploadResult:
        """Instagram Reels에 영상을 업로드하고 UploadResult를 반환한다."""
        if self.settings.dry_run:
            log.info("dry_run.instagram_upload", script_id=video.script_id)
            return UploadResult(
                platform="instagram",
                external_id=f"ig_{video.script_id}",
                url=f"https://instagram.com/reel/{video.script_id}",
            )

        # 필수 키 검증 (주입 클라이언트가 없을 때만)
        if self._ig_client is None:
            if not _usable_key(self.settings.instagram_access_token):
                raise ValueError(
                    "INSTAGRAM_ACCESS_TOKEN이 비어 있습니다 — dry_run=False 시 필수입니다."
                )
            if not _usable_key(self.settings.instagram_account_id):
                raise ValueError(
                    "INSTAGRAM_ACCOUNT_ID가 비어 있습니다 — dry_run=False 시 필수입니다."
                )

        # 주입 클라이언트: 호출자가 수명 관리 → close 금지.
        # 내부 생성 클라이언트: Publisher가 소유 → try/finally로 반드시 close.
        _own_ig = self._ig_client is None
        client = self._ig_client or InstagramClient(self.settings)
        try:
            # 1) 컨테이너 생성
            creation_id = client.create_container(video, meta)
            log.info("instagram.container.created", creation_id=creation_id)

            # 2) 컨테이너 처리 완료까지 폴링 (wall-clock 기반 타임아웃)
            start = self._clock()
            while self._clock() - start < INSTAGRAM_POLL_TIMEOUT_SEC:
                status = client.poll_container(creation_id)
                log.debug("instagram.container.status", creation_id=creation_id, status=status)
                if status == "FINISHED":
                    break
                if status in ("ERROR", "EXPIRED"):
                    raise PublishError(
                        f"Instagram 컨테이너 처리 실패: status={status} creation_id={creation_id}"
                    )
                # PUBLISHED(이미 게시됨)도 진행 가능하나, 일반적으로는 FINISHED를 기다린다
                if status == "PUBLISHED":
                    break
                self._sleep(INSTAGRAM_POLL_INTERVAL_SEC)
            else:
                raise PublishTimeoutError(
                    f"Instagram 컨테이너 폴링 타임아웃({INSTAGRAM_POLL_TIMEOUT_SEC:.0f}s): "
                    f"creation_id={creation_id}"
                )

            # 3) 게시
            media_id = client.publish(creation_id)
            log.info("instagram.published", media_id=media_id)

            # 4) permalink 조회
            permalink = client.fetch_permalink(media_id)
        finally:
            if _own_ig:
                client.close()

        url = permalink or f"https://www.instagram.com/reel/{media_id}/"
        return UploadResult(
            platform="instagram",
            external_id=media_id,
            url=url,
        )

    def fetch_performance(self, upload: UploadResult) -> PerformanceReport:
        """업로드된 콘텐츠의 성과 지표를 조회해 PerformanceReport를 반환한다."""
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

        if upload.platform == "youtube":
            return self._fetch_youtube_performance(upload)
        if upload.platform == "instagram":
            return self._fetch_instagram_performance(upload)
        raise PublishError(f"지원하지 않는 플랫폼입니다: {upload.platform}")

    def _fetch_youtube_performance(self, upload: UploadResult) -> PerformanceReport:
        """YouTube Analytics API로 성과 지표를 조회한다."""
        # 주입 클라이언트: close 금지. 내부 생성: try/finally로 close.
        _own_yt = self._yt_client is None
        client = self._yt_client or YouTubeClient(self.settings)
        try:
            data = client.fetch_analytics(upload.external_id)
        finally:
            if _own_yt:
                client.close()
        # 방어적 파싱: 없는 키는 0으로 정규화
        return PerformanceReport(
            platform="youtube",
            external_id=upload.external_id,
            views=int(data.get("views", 0)),
            likes=int(data.get("likes", 0)),
            comments=int(data.get("comments", 0)),
            avg_view_duration_sec=float(data.get("averageViewDuration", 0)),
        )

    def _fetch_instagram_performance(self, upload: UploadResult) -> PerformanceReport:
        """Instagram Insights API로 Reels 성과 지표를 조회한다.

        TODO(live): 게시 직후에는 지표가 채워지지 않으므로, 수십 분 후 조회 권장.
        ig_reels_avg_watch_time은 ms 단위이므로 1000으로 나눠 초로 변환한다.
        """
        # 주입 클라이언트: close 금지. 내부 생성: try/finally로 close.
        _own_ig = self._ig_client is None
        client = self._ig_client or InstagramClient(self.settings)
        try:
            data = client.fetch_insights(upload.external_id)
        finally:
            if _own_ig:
                client.close()
        # ig_reels_avg_watch_time: ms 단위 → 초 변환
        avg_watch_ms = float(data.get("ig_reels_avg_watch_time", 0))
        return PerformanceReport(
            platform="instagram",
            external_id=upload.external_id,
            views=int(data.get("views", 0)),
            likes=int(data.get("likes", 0)),
            comments=int(data.get("comments", 0)),
            avg_view_duration_sec=avg_watch_ms / 1000.0,
        )
