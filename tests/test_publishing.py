"""Publisher(YouTube/Instagram 업로드·성과 수집) 단위 테스트.

모든 테스트는 fake 클라이언트 주입 또는 dry_run으로 **네트워크 없이** 동작한다.
conftest._block_real_network autouse 픽스처가 실제 httpx 전송을 차단한다.
"""

from __future__ import annotations

from collections import deque

import httpx
import pytest

from nutti.config import Settings
from nutti.integrations.publishing import (
    INSTAGRAM_POLL_TIMEOUT_SEC,
    InstagramClient,
    PublishError,
    PublishTimeoutError,
    Publisher,
    YouTubeClient,
    _usable_key,
)
from nutti.models import Metadata, UploadResult, VideoAsset


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------


def _dry_settings(**overrides) -> Settings:
    """dry_run=True 설정. alias 키를 사용해야 pydantic-settings가 올바르게 적용된다."""
    base: dict = {"NUTTI_DRY_RUN": True}
    base.update(overrides)
    return Settings(**base)


def _live_settings(**overrides) -> Settings:
    """dry_run=False 설정. 실제 네트워크는 fake 주입으로 차단한다."""
    base: dict = {"NUTTI_DRY_RUN": False}
    base.update(overrides)
    return Settings(**base)


def _video(script_id: str = "abc123") -> VideoAsset:
    """테스트용 최소 VideoAsset."""
    return VideoAsset(
        script_id=script_id,
        final_url="https://fake.local/final/abc123.mp4",
    )


def _meta(title: str = "테스트 영상") -> Metadata:
    """테스트용 최소 Metadata."""
    return Metadata(title=title, description="간식 설명", hashtags=["강아지", "간식"])


def _no_sleep(_seconds: float) -> None:
    """폴링 대기 없이 즉시 반환하는 가짜 sleep(결정적 시간 제어)."""


# ---------------------------------------------------------------------------
# Fake 클라이언트
# ---------------------------------------------------------------------------


class FakeYouTubeClient:
    """YouTubeClient 실 클라이언트 대체.

    exchange_token/upload_video/fetch_analytics 호출을 기록하고 결정적 값을 반환한다.
    raise_on_exchange=True 설정 시 exchange_token()에서 PublishError를 던진다.
    raise_on_upload=True 설정 시 upload_video()에서 PublishError를 던진다.
    """

    def __init__(
        self,
        *,
        token: str = "fake_access_token",
        video_id: str = "yt_video_001",
        analytics: dict | None = None,
        raise_on_exchange: bool = False,
        raise_on_upload: bool = False,
    ):
        self._token = token
        self._video_id = video_id
        # analytics=None이면 기본 더미, 명시적으로 넘기면 그 값을 그대로 사용
        self._analytics = (
            analytics
            if analytics is not None
            else {"views": 500, "likes": 20, "comments": 3, "averageViewDuration": 25.0}
        )
        self.raise_on_exchange = raise_on_exchange
        self.raise_on_upload = raise_on_upload
        # 호출 기록
        self.exchange_calls: list[None] = []
        self.upload_calls: list[tuple] = []
        self.analytics_calls: list[str] = []

    def exchange_token(self) -> str:
        self.exchange_calls.append(None)
        if self.raise_on_exchange:
            raise PublishError("YouTube OAuth 토큰 교환 HTTP 401")
        return self._token

    def upload_video(self, video: VideoAsset, meta: Metadata, access_token: str) -> str:
        self.upload_calls.append((video, meta, access_token))
        if self.raise_on_upload:
            raise PublishError("YouTube 업로드 HTTP 403")
        return self._video_id

    def fetch_analytics(self, external_id: str) -> dict:
        self.analytics_calls.append(external_id)
        return self._analytics


class FakeInstagramClient:
    """InstagramClient 실 클라이언트 대체.

    create_container/poll_container/publish/fetch_permalink/fetch_insights 호출을
    기록하고 결정적 값을 반환한다.
    poll_statuses: 큐 기반 상태 반환(deque). 비면 "FINISHED"를 반환한다.
    raise_on_create=True 설정 시 create_container()에서 PublishError를 던진다.
    raise_on_publish=True 설정 시 publish()에서 PublishError를 던진다.
    """

    def __init__(
        self,
        *,
        creation_id: str = "ig_container_001",
        media_id: str = "ig_media_001",
        permalink: str = "https://www.instagram.com/reel/ig_media_001/",
        poll_statuses: list[str] | None = None,
        insights: dict | None = None,
        raise_on_create: bool = False,
        raise_on_publish: bool = False,
    ):
        self._creation_id = creation_id
        self._media_id = media_id
        self._permalink = permalink
        # deque를 소비하면서 순차 상태를 반환, 소진하면 "FINISHED"
        self._poll_queue: deque[str] = deque(poll_statuses or [])
        # insights=None이면 기본 더미, insights={}이면 빈 dict(명시적 빈 결과 테스트용)
        self._insights = (
            insights
            if insights is not None
            else {"views": 300, "likes": 15, "comments": 2, "ig_reels_avg_watch_time": 18500}
        )
        self.raise_on_create = raise_on_create
        self.raise_on_publish = raise_on_publish
        # 호출 기록
        self.create_calls: list[tuple] = []
        self.poll_calls: list[str] = []
        self.publish_calls: list[str] = []
        self.permalink_calls: list[str] = []
        self.insights_calls: list[str] = []

    def create_container(self, video: VideoAsset, meta: Metadata) -> str:
        self.create_calls.append((video, meta))
        if self.raise_on_create:
            raise PublishError("Instagram 컨테이너 생성 HTTP 400")
        return self._creation_id

    def poll_container(self, creation_id: str) -> str:
        self.poll_calls.append(creation_id)
        if self._poll_queue:
            return self._poll_queue.popleft()
        return "FINISHED"

    def publish(self, creation_id: str) -> str:
        self.publish_calls.append(creation_id)
        if self.raise_on_publish:
            raise PublishError("Instagram 게시 HTTP 500")
        return self._media_id

    def fetch_permalink(self, media_id: str) -> str:
        self.permalink_calls.append(media_id)
        return self._permalink

    def fetch_insights(self, media_id: str) -> dict:
        self.insights_calls.append(media_id)
        return self._insights


# ---------------------------------------------------------------------------
# 유틸 함수 테스트
# ---------------------------------------------------------------------------


def test_usable_key_empty():
    """빈 문자열은 usable하지 않다."""
    assert not _usable_key("")
    assert not _usable_key(None)
    assert not _usable_key("  ")


def test_usable_key_comment():
    """주석 문자열(# 시작)은 usable하지 않다."""
    assert not _usable_key("# 여기 키 입력")
    assert not _usable_key("  # 설명")


def test_usable_key_valid():
    """실제 키 값은 usable하다."""
    assert _usable_key("abc123")
    assert _usable_key("  my_key  ")


# ---------------------------------------------------------------------------
# YouTube dry_run 테스트
# ---------------------------------------------------------------------------


def test_youtube_dry_run_returns_dummy():
    """dry_run=True이면 네트워크 없이 즉시 UploadResult를 반환한다."""
    settings = _dry_settings()
    publisher = Publisher(settings)
    video = _video("script001")
    meta = _meta()

    result = publisher.upload_youtube(video, meta)

    assert result.platform == "youtube"
    assert result.external_id == "yt_script001"
    assert "script001" in result.url


# ---------------------------------------------------------------------------
# YouTube 라이브 경로 테스트 (fake 주입)
# ---------------------------------------------------------------------------


def test_youtube_upload_calls_exchange_then_upload():
    """라이브 경로에서 exchange_token → upload_video 순으로 호출하고 UploadResult를 반환한다."""
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
    )
    fake_yt = FakeYouTubeClient(token="tok_abc", video_id="video_xyz")
    publisher = Publisher(settings, yt_client=fake_yt)

    result = publisher.upload_youtube(_video(), _meta())

    # exchange_token이 먼저 한 번 호출됐는지 확인
    assert len(fake_yt.exchange_calls) == 1
    # upload_video가 올바른 access_token으로 호출됐는지 확인
    assert len(fake_yt.upload_calls) == 1
    _, _, access_token_used = fake_yt.upload_calls[0]
    assert access_token_used == "tok_abc"
    # 반환값 검증
    assert result.platform == "youtube"
    assert result.external_id == "video_xyz"
    assert "video_xyz" in result.url


def test_youtube_http_error_on_exchange_propagates():
    """exchange_token()에서 PublishError가 발생하면 upload_youtube에서 전파된다."""
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
    )
    fake_yt = FakeYouTubeClient(raise_on_exchange=True)
    publisher = Publisher(settings, yt_client=fake_yt)

    with pytest.raises(PublishError, match="401"):
        publisher.upload_youtube(_video(), _meta())

    # exchange 호출은 됐지만 upload는 호출되지 않음
    assert len(fake_yt.exchange_calls) == 1
    assert len(fake_yt.upload_calls) == 0


def test_youtube_upload_error_propagates():
    """upload_video()에서 PublishError가 발생하면 전파된다."""
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
    )
    fake_yt = FakeYouTubeClient(raise_on_upload=True)
    publisher = Publisher(settings, yt_client=fake_yt)

    with pytest.raises(PublishError, match="403"):
        publisher.upload_youtube(_video(), _meta())


def test_youtube_missing_keys_raise_value_error():
    """dry_run=False + 클라이언트 미주입 + OAuth 키 없음 → ValueError.

    upload_youtube의 필수 키 검증 가드(publishing.py:456-466)가 실제로 동작함을 검증한다.
    이 테스트가 없으면 가드 블록이 삭제·약화되어도 pytest가 통과해버려 misconfiguration이
    운영 HTTP 클라이언트까지 전파된다.
    """
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="",
        YOUTUBE_CLIENT_SECRET="",
        YOUTUBE_REFRESH_TOKEN="",
    )
    # yt_client를 주입하지 않아야 가드가 평가된다.
    publisher = Publisher(settings)

    with pytest.raises(ValueError, match="YOUTUBE_CLIENT_ID"):
        publisher.upload_youtube(_video(), _meta())


def test_instagram_missing_keys_raise_value_error():
    """dry_run=False + 클라이언트 미주입 + 액세스 토큰/계정 ID 없음 → ValueError.

    upload_instagram의 필수 키 검증 가드(publishing.py:492-498)가 실제로 동작함을 검증한다.
    이 테스트가 없으면 가드 블록이 삭제·약화되어도 pytest가 통과해버린다.
    """
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="",
        INSTAGRAM_ACCOUNT_ID="",
    )
    # ig_client를 주입하지 않아야 가드가 평가된다.
    publisher = Publisher(settings)

    with pytest.raises(ValueError, match="INSTAGRAM_ACCESS_TOKEN"):
        publisher.upload_instagram(_video(), _meta())


def test_youtube_redaction_does_not_leak_access_token():
    """PublishError 메시지에 access_token 값이 포함되지 않는다.

    _raise_for_publish는 상태 코드만 노출하므로, 에러 메시지에서 실제 토큰 값이
    새지 않음을 검증한다.
    """
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
    )
    fake_yt = FakeYouTubeClient(token="SUPER_SECRET_TOKEN", raise_on_upload=True)
    publisher = Publisher(settings, yt_client=fake_yt)

    with pytest.raises(PublishError) as exc_info:
        publisher.upload_youtube(_video(), _meta())

    assert "SUPER_SECRET_TOKEN" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Instagram dry_run 테스트
# ---------------------------------------------------------------------------


def test_instagram_dry_run_returns_dummy():
    """dry_run=True이면 네트워크 없이 즉시 UploadResult를 반환한다."""
    settings = _dry_settings()
    publisher = Publisher(settings)
    video = _video("script002")

    result = publisher.upload_instagram(video, _meta())

    assert result.platform == "instagram"
    assert result.external_id == "ig_script002"
    assert "script002" in result.url


# ---------------------------------------------------------------------------
# Instagram 라이브 경로 테스트 (fake 주입)
# ---------------------------------------------------------------------------


def test_instagram_upload_full_flow():
    """라이브 경로에서 create → poll(IN_PROGRESS → FINISHED) → publish → permalink 전체 플로우."""
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_ig = FakeInstagramClient(
        creation_id="container_001",
        media_id="media_001",
        permalink="https://www.instagram.com/reel/media_001/",
        # 처음에는 IN_PROGRESS, 두 번째에서 FINISHED
        poll_statuses=["IN_PROGRESS", "FINISHED"],
    )
    publisher = Publisher(settings, ig_client=fake_ig, sleep=_no_sleep)

    result = publisher.upload_instagram(_video(), _meta())

    # create_container 호출 확인
    assert len(fake_ig.create_calls) == 1
    # poll_container 2회 호출 확인(IN_PROGRESS → FINISHED)
    assert len(fake_ig.poll_calls) == 2
    assert fake_ig.poll_calls == ["container_001", "container_001"]
    # publish 호출 확인
    assert len(fake_ig.publish_calls) == 1
    assert fake_ig.publish_calls[0] == "container_001"
    # permalink 조회 확인
    assert len(fake_ig.permalink_calls) == 1
    # 결과 검증
    assert result.platform == "instagram"
    assert result.external_id == "media_001"
    assert result.url == "https://www.instagram.com/reel/media_001/"


def test_instagram_upload_finished_immediately():
    """poll_container가 첫 번째 호출에서 바로 FINISHED를 반환하는 경우."""
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_ig = FakeInstagramClient(poll_statuses=["FINISHED"])
    publisher = Publisher(settings, ig_client=fake_ig, sleep=_no_sleep)

    result = publisher.upload_instagram(_video(), _meta())

    assert len(fake_ig.poll_calls) == 1
    assert result.platform == "instagram"


def test_instagram_clock_based_timeout_is_load_bearing():
    """clock 주입으로 타임아웃을 제어할 수 있음을 검증한다(clock 기반 타임아웃 회귀 방지).

    clock이 start=0, 첫 while 통과(0), poll 1회, 두 번째 while에서 초과 → 타임아웃.
    counter 기반이었다면 100개의 IN_PROGRESS를 다 소진한 후 FINISHED를 반환해 성공했을 것이다.
    이 테스트는 clock 기반 구현이 없으면 PublishTimeoutError가 발생하지 않아 실패한다.
    """
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    # tick 0: start(=0.0), tick 1: 첫 while check(0<TIMEOUT → 통과), tick 2: 두 번째 while check(초과)
    ticks = iter([0.0, 0.0, INSTAGRAM_POLL_TIMEOUT_SEC + 1.0])
    fake_ig = FakeInstagramClient(
        poll_statuses=["IN_PROGRESS"] * 100,  # 많이 줘도 clock이 1회 후 차단
    )
    publisher = Publisher(
        settings,
        ig_client=fake_ig,
        clock=lambda: next(ticks),
        sleep=_no_sleep,
    )

    with pytest.raises(PublishTimeoutError):
        publisher.upload_instagram(_video(), _meta())

    # clock 기반이면 IN_PROGRESS 정확히 1번만 폴링하고 타임아웃
    assert len(fake_ig.poll_calls) == 1


def test_instagram_container_error_status_raises():
    """컨테이너 상태가 ERROR이면 PublishError를 발생시킨다."""
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_ig = FakeInstagramClient(poll_statuses=["IN_PROGRESS", "ERROR"])
    publisher = Publisher(settings, ig_client=fake_ig, sleep=_no_sleep)

    with pytest.raises(PublishError, match="ERROR"):
        publisher.upload_instagram(_video(), _meta())


def test_instagram_container_expired_status_raises():
    """컨테이너 상태가 EXPIRED이면 PublishError를 발생시킨다."""
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_ig = FakeInstagramClient(poll_statuses=["EXPIRED"])
    publisher = Publisher(settings, ig_client=fake_ig, sleep=_no_sleep)

    with pytest.raises(PublishError, match="EXPIRED"):
        publisher.upload_instagram(_video(), _meta())


def test_instagram_create_container_error_propagates():
    """create_container()에서 PublishError 발생 시 전파된다."""
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_ig = FakeInstagramClient(raise_on_create=True)
    publisher = Publisher(settings, ig_client=fake_ig, sleep=_no_sleep)

    with pytest.raises(PublishError, match="400"):
        publisher.upload_instagram(_video(), _meta())

    # poll/publish는 호출되지 않음
    assert len(fake_ig.poll_calls) == 0
    assert len(fake_ig.publish_calls) == 0


def test_instagram_publish_error_propagates():
    """publish()에서 PublishError 발생 시 전파된다."""
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_ig = FakeInstagramClient(poll_statuses=["FINISHED"], raise_on_publish=True)
    publisher = Publisher(settings, ig_client=fake_ig, sleep=_no_sleep)

    with pytest.raises(PublishError, match="500"):
        publisher.upload_instagram(_video(), _meta())


def test_instagram_redaction_does_not_leak_token():
    """PublishError 메시지에 access_token 값이 포함되지 않는다.

    _raise_for_publish는 상태 코드만 노출하며, URL에 토큰이 포함되더라도
    에러 메시지에서 직접 토큰 값이 새지 않도록 설계되어 있음을 검증한다.
    """
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="MY_SECRET_IG_TOKEN",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_ig = FakeInstagramClient(raise_on_create=True)
    publisher = Publisher(settings, ig_client=fake_ig, sleep=_no_sleep)

    with pytest.raises(PublishError) as exc_info:
        publisher.upload_instagram(_video(), _meta())

    # 에러 메시지에 토큰 값이 노출되지 않아야 함
    assert "MY_SECRET_IG_TOKEN" not in str(exc_info.value)


def test_instagram_create_container_transport_error_does_not_leak_token():
    """create_container()에서 TransportError 발생 시 access_token이 PublishError에 노출되지 않는다.

    httpx.TransportError는 request 객체(POST 바디에 access_token 포함)를 담고 있으나,
    래핑된 PublishError 메시지에는 토큰이 포함되지 않아야 한다.
    이 테스트가 없으면 try/except 제거 시 토큰이 노출되어 실패한다.
    """
    secret_token = "SUPER_SECRET_IG_ACCESS_TOKEN"
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN=secret_token,
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    # POST 바디에 access_token이 포함된 요청을 시뮬레이션하는 fake HTTP 클라이언트
    fake_request = httpx.Request(
        "POST",
        "https://graph.facebook.com/v25.0/ig_acc_123/media",
        data={"access_token": secret_token, "media_type": "REELS"},
    )
    raising_http = FakeRaisingHttpClient(fake_request)
    client = InstagramClient(settings, http=raising_http)

    with pytest.raises(PublishError) as exc_info:
        client.create_container(_video(), _meta())

    assert secret_token not in str(exc_info.value)


def test_instagram_publish_transport_error_does_not_leak_token():
    """publish()에서 TransportError 발생 시 access_token이 PublishError에 노출되지 않는다."""
    secret_token = "PUBLISH_SECRET_TOKEN"
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN=secret_token,
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_request = httpx.Request(
        "POST",
        "https://graph.facebook.com/v25.0/ig_acc_123/media_publish",
        data={"access_token": secret_token, "creation_id": "cid_001"},
    )
    raising_http = FakeRaisingHttpClient(fake_request)
    client = InstagramClient(settings, http=raising_http)

    with pytest.raises(PublishError) as exc_info:
        client.publish("cid_001")

    assert secret_token not in str(exc_info.value)


def test_instagram_poll_container_transport_error_does_not_leak_token():
    """poll_container()에서 TransportError 발생 시 access_token이 PublishError에 노출되지 않는다."""
    secret_token = "POLL_SECRET_TOKEN"
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN=secret_token,
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    # access_token이 쿼리 파라미터로 포함된 URL을 시뮬레이션
    fake_request = httpx.Request(
        "GET",
        f"https://graph.facebook.com/v25.0/cid_001?fields=status_code&access_token={secret_token}",
    )
    raising_http = FakeRaisingHttpClient(fake_request)
    client = InstagramClient(settings, http=raising_http)

    with pytest.raises(PublishError) as exc_info:
        client.poll_container("cid_001")

    assert secret_token not in str(exc_info.value)


def test_instagram_fetch_permalink_transport_error_does_not_leak_token():
    """fetch_permalink()에서 TransportError 발생 시 access_token이 PublishError에 노출되지 않는다."""
    secret_token = "PERMALINK_SECRET_TOKEN"
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN=secret_token,
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_request = httpx.Request(
        "GET",
        f"https://graph.facebook.com/v25.0/media_001?fields=permalink&access_token={secret_token}",
    )
    raising_http = FakeRaisingHttpClient(fake_request)
    client = InstagramClient(settings, http=raising_http)

    with pytest.raises(PublishError) as exc_info:
        client.fetch_permalink("media_001")

    assert secret_token not in str(exc_info.value)


def test_instagram_fetch_insights_transport_error_does_not_leak_token():
    """fetch_insights()에서 TransportError 발생 시 access_token이 PublishError에 노출되지 않는다."""
    secret_token = "INSIGHTS_SECRET_TOKEN"
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN=secret_token,
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    fake_request = httpx.Request(
        "GET",
        f"https://graph.facebook.com/v25.0/media_001/insights?metric=views&access_token={secret_token}",
    )
    raising_http = FakeRaisingHttpClient(fake_request)
    client = InstagramClient(settings, http=raising_http)

    with pytest.raises(PublishError) as exc_info:
        client.fetch_insights("media_001")

    assert secret_token not in str(exc_info.value)


def test_youtube_exchange_token_transport_error_does_not_leak_credentials():
    """exchange_token()에서 TransportError 발생 시 client_secret/refresh_token이 노출되지 않는다.

    httpx.TransportError는 request 객체(POST 바디에 시크릿 포함)를 담고 있으나,
    래핑된 PublishError 메시지에는 자격증명이 포함되지 않아야 한다.
    """
    secret_key = "MY_CLIENT_SECRET_VALUE"
    refresh_tok = "MY_REFRESH_TOKEN_VALUE"
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET=secret_key,
        YOUTUBE_REFRESH_TOKEN=refresh_tok,
    )
    fake_request = httpx.Request(
        "POST",
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": "cid",
            "client_secret": secret_key,
        },
    )
    raising_http = FakeRaisingHttpClient(fake_request)
    client = YouTubeClient(settings, http=raising_http)

    with pytest.raises(PublishError) as exc_info:
        client.exchange_token()

    err_msg = str(exc_info.value)
    assert secret_key not in err_msg
    assert refresh_tok not in err_msg


# ---------------------------------------------------------------------------
# fetch_performance 테스트
# ---------------------------------------------------------------------------


def test_fetch_performance_dry_run_youtube():
    """dry_run=True이면 YouTube 더미 PerformanceReport를 반환한다."""
    settings = _dry_settings()
    publisher = Publisher(settings)
    upload = UploadResult(platform="youtube", external_id="yt_abc", url="https://youtube.com/s/yt_abc")

    report = publisher.fetch_performance(upload)

    assert report.platform == "youtube"
    assert report.external_id == "yt_abc"
    assert report.views == 1234
    assert report.likes == 88


def test_fetch_performance_dry_run_instagram():
    """dry_run=True이면 Instagram 더미 PerformanceReport를 반환한다."""
    settings = _dry_settings()
    publisher = Publisher(settings)
    upload = UploadResult(
        platform="instagram",
        external_id="ig_abc",
        url="https://instagram.com/reel/ig_abc",
    )

    report = publisher.fetch_performance(upload)

    assert report.platform == "instagram"
    assert report.external_id == "ig_abc"
    assert report.views == 1234


def test_fetch_performance_live_youtube_path():
    """라이브 경로에서 YouTubeClient.fetch_analytics를 호출하고 PerformanceReport를 반환한다."""
    settings = _live_settings()
    fake_yt = FakeYouTubeClient(
        analytics={"views": 999, "likes": 42, "comments": 7, "averageViewDuration": 35.5}
    )
    publisher = Publisher(settings, yt_client=fake_yt)
    upload = UploadResult(
        platform="youtube", external_id="video_123", url="https://youtube.com/s/video_123"
    )

    report = publisher.fetch_performance(upload)

    assert len(fake_yt.analytics_calls) == 1
    assert fake_yt.analytics_calls[0] == "video_123"
    assert report.platform == "youtube"
    assert report.views == 999
    assert report.likes == 42
    assert report.comments == 7
    assert report.avg_view_duration_sec == 35.5


def test_fetch_performance_live_instagram_path():
    """라이브 경로에서 InstagramClient.fetch_insights를 호출하고 PerformanceReport를 반환한다.

    ig_reels_avg_watch_time은 ms 단위이므로 1000으로 나눠 초로 변환한다.
    """
    settings = _live_settings()
    fake_ig = FakeInstagramClient(
        insights={
            "views": 500,
            "likes": 30,
            "comments": 5,
            "ig_reels_avg_watch_time": 18500,  # 18.5초 = 18500ms
        }
    )
    publisher = Publisher(settings, ig_client=fake_ig)
    upload = UploadResult(
        platform="instagram",
        external_id="media_456",
        url="https://www.instagram.com/reel/media_456/",
    )

    report = publisher.fetch_performance(upload)

    assert len(fake_ig.insights_calls) == 1
    assert fake_ig.insights_calls[0] == "media_456"
    assert report.platform == "instagram"
    assert report.views == 500
    assert report.likes == 30
    assert report.comments == 5
    # ms → 초 변환 확인
    assert report.avg_view_duration_sec == pytest.approx(18.5)


def test_fetch_performance_instagram_empty_insights():
    """fetch_insights가 빈 dict를 반환해도 0으로 정규화해 PerformanceReport를 반환한다."""
    settings = _live_settings()
    fake_ig = FakeInstagramClient(insights={})
    publisher = Publisher(settings, ig_client=fake_ig)
    upload = UploadResult(
        platform="instagram",
        external_id="media_empty",
        url="https://www.instagram.com/reel/media_empty/",
    )

    report = publisher.fetch_performance(upload)

    assert report.views == 0
    assert report.likes == 0
    assert report.avg_view_duration_sec == 0.0


def test_fetch_performance_unsupported_platform():
    """지원하지 않는 플랫폼이면 PublishError를 발생시킨다."""
    settings = _live_settings()
    publisher = Publisher(settings)
    upload = UploadResult(
        platform="tiktok", external_id="tok_abc", url="https://tiktok.com/tok_abc"
    )

    with pytest.raises(PublishError, match="tiktok"):
        publisher.fetch_performance(upload)


# ---------------------------------------------------------------------------
# InstagramClient 내부 단위 테스트 — fetch_insights 응답 파싱
# ---------------------------------------------------------------------------


class FakeHttpResponse:
    """httpx.Response 최소 fake(상태 코드 + json() + headers + content)."""

    def __init__(
        self,
        status_code: int = 200,
        body: dict | None = None,
        headers: dict | None = None,
        content: bytes = b"",
    ):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}
        self.content = content

    def json(self) -> dict:
        return self._body


class FakeHttpClient:
    """httpx.Client 최소 fake. get/post/put 호출을 응답 큐로 처리한다."""

    def __init__(self, responses: list[FakeHttpResponse]):
        self._responses: deque[FakeHttpResponse] = deque(responses)
        self.get_calls: list[tuple] = []
        self.post_calls: list[tuple] = []
        self.put_calls: list[tuple] = []

    def get(self, url: str, **kwargs) -> FakeHttpResponse:
        self.get_calls.append((url, kwargs))
        return self._responses.popleft()

    def post(self, url: str, **kwargs) -> FakeHttpResponse:
        self.post_calls.append((url, kwargs))
        return self._responses.popleft()

    def put(self, url: str, **kwargs) -> FakeHttpResponse:
        self.put_calls.append((url, kwargs))
        return self._responses.popleft()


class FakeRaisingHttpClient:
    """모든 get/post 호출에서 httpx.TransportError를 발생시키는 fake HTTP 클라이언트.

    실제 전송 계층 오류 경로(redaction 포함)를 검증하기 위해 사용한다.
    `request` 인자는 httpx.Request 객체로, 민감 정보(토큰/시크릿)가 포함된 URL/바디를 가진다.
    """

    def __init__(self, request: httpx.Request):
        self._request = request

    def get(self, url: str, **kwargs) -> None:
        raise httpx.TransportError("연결 오류", request=self._request)

    def post(self, url: str, **kwargs) -> None:
        raise httpx.TransportError("연결 오류", request=self._request)


def test_instagram_fetch_insights_parses_response():
    """fetch_insights가 data[] 배열 구조를 올바르게 파싱해 dict를 반환한다."""
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="tok",
        INSTAGRAM_ACCOUNT_ID="acc",
    )
    fake_response = FakeHttpResponse(
        status_code=200,
        body={
            "data": [
                {"name": "views", "values": [{"value": 200}]},
                {"name": "likes", "values": [{"value": 10}]},
                {"name": "ig_reels_avg_watch_time", "values": [{"value": 12000}]},
            ]
        },
    )
    client = InstagramClient(settings, http=FakeHttpClient([fake_response]))

    result = client.fetch_insights("media_999")

    assert result["views"] == 200
    assert result["likes"] == 10
    assert result["ig_reels_avg_watch_time"] == 12000


def test_instagram_fetch_insights_http_error():
    """fetch_insights가 HTTP 400 응답 시 PublishError를 발생시킨다."""
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="tok",
        INSTAGRAM_ACCOUNT_ID="acc",
    )
    fake_response = FakeHttpResponse(status_code=400, body={"error": "invalid"})
    client = InstagramClient(settings, http=FakeHttpClient([fake_response]))

    with pytest.raises(PublishError, match="400"):
        client.fetch_insights("media_bad")


def test_youtube_client_exchange_token_http_error():
    """exchange_token이 HTTP 401 응답 시 PublishError를 발생시킨다."""
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
    )
    fake_response = FakeHttpResponse(
        status_code=401,
        body={"error": "invalid_client", "error_description": "The OAuth client was not found."},
    )
    client = YouTubeClient(settings, http=FakeHttpClient([fake_response]))

    with pytest.raises(PublishError, match="401"):
        client.exchange_token()


def test_youtube_client_exchange_token_missing_access_token():
    """exchange_token 응답에 access_token 키가 없으면 PublishError를 발생시킨다."""
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
    )
    # HTTP 200이지만 access_token 필드 없음
    fake_response = FakeHttpResponse(status_code=200, body={"token_type": "Bearer"})
    client = YouTubeClient(settings, http=FakeHttpClient([fake_response]))

    with pytest.raises(PublishError, match="access_token"):
        client.exchange_token()


# ---------------------------------------------------------------------------
# YouTubeClient.upload_video — resumable upload(initiation POST → PUT 바이트) 단위 테스트
# ---------------------------------------------------------------------------


_SESSION_URI = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=xyz"


def _yt_live_settings() -> Settings:
    return _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
    )


def _local_video(tmp_path, data: bytes = b"FAKE_MP4_BYTES") -> VideoAsset:
    """실제 로컬 파일을 가진 VideoAsset(업로드 바이트 소스)."""
    p = tmp_path / "clip.mp4"
    p.write_bytes(data)
    return VideoAsset(script_id="abc123", video_path=str(p), final_url=str(p))


def test_youtube_upload_video_resumable_success(tmp_path):
    """initiation POST(Location 헤더) → 로컬 파일 PUT → 응답 id를 video_id로 반환한다."""
    data = b"HELLO_VIDEO_BYTES"
    video = _local_video(tmp_path, data)
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI}),
            FakeHttpResponse(status_code=200, body={"id": "yt_real_id"}),
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    video_id = client.upload_video(video, _meta(), "access_tok")

    assert video_id == "yt_real_id"
    # initiation은 POST 1회, 바이트 전송은 PUT 1회
    assert len(http.post_calls) == 1
    assert len(http.put_calls) == 1
    # PUT은 세션 URI로, 실제 바이트를 content로 보냈는지 확인
    put_url, put_kwargs = http.put_calls[0]
    assert put_url == _SESSION_URI
    assert put_kwargs["content"] == data
    assert put_kwargs["headers"]["Content-Length"] == str(len(data))


def test_youtube_upload_video_privacy_status_from_settings(tmp_path):
    """initiation POST의 privacyStatus가 설정값을 따른다(검증용 private override 핀)."""
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
        NUTTI_YOUTUBE_PRIVACY_STATUS="private",
    )
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI}),
            FakeHttpResponse(status_code=200, body={"id": "yt_priv"}),
        ]
    )
    client = YouTubeClient(settings, http=http)

    client.upload_video(_local_video(tmp_path), _meta(), "access_tok")

    _, post_kwargs = http.post_calls[0]
    assert post_kwargs["json"]["status"]["privacyStatus"] == "private"


def test_youtube_upload_video_privacy_status_defaults_public(tmp_path):
    """설정이 없으면 운영 기본값 public을 사용한다."""
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI}),
            FakeHttpResponse(status_code=200, body={"id": "yt_pub"}),
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    client.upload_video(_local_video(tmp_path), _meta(), "access_tok")

    _, post_kwargs = http.post_calls[0]
    assert post_kwargs["json"]["status"]["privacyStatus"] == "public"


def test_youtube_privacy_status_rejects_invalid_value():
    """오타 등 허용되지 않은 privacy 값은 Settings 생성 시점에 ValidationError로 잡는다."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _live_settings(NUTTI_YOUTUBE_PRIVACY_STATUS="privat")


def test_youtube_upload_video_algo_metadata_in_snippet(tmp_path):
    """알고리즘 최적화 메타데이터(카테고리·언어·madeForKids·태그 # 제거)가 업로드 body에 실린다."""
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
        NUTTI_YOUTUBE_CATEGORY_ID="15",
        NUTTI_YOUTUBE_DEFAULT_LANGUAGE="ko",
        NUTTI_YOUTUBE_MADE_FOR_KIDS=False,
    )
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI}),
            FakeHttpResponse(status_code=200, body={"id": "yt_algo"}),
        ]
    )
    client = YouTubeClient(settings, http=http)
    meta = Metadata(title="t", description="d", hashtags=["#강아지", "#Shorts", "  "])

    client.upload_video(_local_video(tmp_path), meta, "access_tok")

    body = http.post_calls[0][1]["json"]
    snippet, status = body["snippet"], body["status"]
    assert snippet["categoryId"] == "15"
    assert snippet["defaultLanguage"] == "ko"
    assert snippet["defaultAudioLanguage"] == "ko"
    # tags는 '#' 제거 + 빈 항목 제외
    assert snippet["tags"] == ["강아지", "Shorts"]
    assert status["selfDeclaredMadeForKids"] is False


def test_youtube_upload_video_missing_location_raises(tmp_path):
    """initiation 응답에 Location 헤더가 없으면 PublishError를 발생시키고 PUT을 시도하지 않는다."""
    http = FakeHttpClient([FakeHttpResponse(status_code=200, headers={})])
    client = YouTubeClient(_yt_live_settings(), http=http)

    with pytest.raises(PublishError, match="Location"):
        client.upload_video(_local_video(tmp_path), _meta(), "access_tok")

    assert len(http.put_calls) == 0


def test_youtube_upload_video_put_http_error_propagates(tmp_path):
    """PUT 단계에서 HTTP 4xx/5xx면 PublishError(상태 코드)를 발생시킨다."""
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI}),
            FakeHttpResponse(status_code=403, body={"error": "forbidden"}),
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    with pytest.raises(PublishError, match="403"):
        client.upload_video(_local_video(tmp_path), _meta(), "access_tok")


def test_youtube_upload_video_missing_id_raises(tmp_path):
    """PUT 응답에 id가 없으면 PublishError를 발생시킨다."""
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI}),
            FakeHttpResponse(status_code=200, body={"kind": "youtube#video"}),
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    with pytest.raises(PublishError, match="id"):
        client.upload_video(_local_video(tmp_path), _meta(), "access_tok")


def test_youtube_upload_video_file_not_found_raises(tmp_path):
    """업로드할 로컬 영상 파일이 없으면 PublishError를 발생시키되 전체 경로는 노출하지 않는다."""
    missing = tmp_path / "secret_dir" / "nope.mp4"
    video = VideoAsset(script_id="abc123", video_path=str(missing), final_url=str(missing))
    http = FakeHttpClient([FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI})])
    client = YouTubeClient(_yt_live_settings(), http=http)

    with pytest.raises(PublishError) as exc_info:
        client.upload_video(video, _meta(), "access_tok")

    msg = str(exc_info.value)
    assert "nope.mp4" in msg  # 파일명은 노출
    assert "secret_dir" not in msg  # 전체 경로는 가림
    # 파일이 없으면 PUT 시도하지 않음
    assert len(http.put_calls) == 0


def test_youtube_upload_video_remote_url_downloads_then_puts():
    """video_path가 비고 final_url이 원격 URL이면 GET 다운로드 후 그 바이트를 PUT한다."""
    remote_bytes = b"REMOTE_DOWNLOADED_BYTES"
    video = VideoAsset(script_id="abc123", final_url="https://cdn.example.com/clip.mp4")
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI}),  # initiation POST
            FakeHttpResponse(status_code=200, content=remote_bytes),  # GET 다운로드
            FakeHttpResponse(status_code=200, body={"id": "yt_remote_id"}),  # PUT
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    video_id = client.upload_video(video, _meta(), "access_tok")

    assert video_id == "yt_remote_id"
    assert len(http.get_calls) == 1  # 원격 다운로드 1회
    put_url, put_kwargs = http.put_calls[0]
    assert put_kwargs["content"] == remote_bytes


def test_youtube_upload_video_no_source_raises():
    """video_path·final_url이 모두 비면 업로드 위치 없음 PublishError를 발생시킨다."""
    video = VideoAsset(script_id="abc123")
    http = FakeHttpClient([FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI})])
    client = YouTubeClient(_yt_live_settings(), http=http)

    with pytest.raises(PublishError, match="영상 위치"):
        client.upload_video(video, _meta(), "access_tok")

    # 바이트 확보는 initiation POST '이후'에 일어난다 — 순서 의존을 핀(M-2 회귀 방지).
    assert len(http.post_calls) == 1
    assert len(http.put_calls) == 0


def test_youtube_upload_video_put_308_resume_incomplete_raises(tmp_path):
    """PUT이 308 Resume Incomplete를 반환하면 미완료 오류로 분리한다(id 누락으로 오분류 금지)."""
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI}),
            FakeHttpResponse(status_code=308, headers={"Range": "bytes=0-99"}),
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    with pytest.raises(PublishError, match="308"):
        client.upload_video(_local_video(tmp_path), _meta(), "access_tok")


def test_youtube_upload_video_lowercase_location_header(tmp_path):
    """initiation 응답의 Location 헤더가 소문자여도 세션 URI를 인식한다(케이스 무관 조회 핀)."""
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, headers={"location": _SESSION_URI}),
            FakeHttpResponse(status_code=200, body={"id": "yt_lower_id"}),
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    video_id = client.upload_video(_local_video(tmp_path), _meta(), "access_tok")

    assert video_id == "yt_lower_id"
    assert http.put_calls[0][0] == _SESSION_URI


def test_youtube_upload_video_put_transport_error_does_not_leak_token(tmp_path):
    """PUT 전송 오류 시 세션 URI/access_token이 PublishError에 노출되지 않는다."""
    secret = "PUT_SESSION_SECRET"
    fake_request = httpx.Request("PUT", f"{_SESSION_URI}&t={secret}")

    class _PutRaisingClient:
        def post(self, url, **kwargs):
            return FakeHttpResponse(status_code=200, headers={"Location": _SESSION_URI})

        def put(self, url, **kwargs):
            raise httpx.TransportError("연결 오류", request=fake_request)

    client = YouTubeClient(_yt_live_settings(), http=_PutRaisingClient())

    with pytest.raises(PublishError) as exc_info:
        client.upload_video(_local_video(tmp_path), _meta(), secret)

    err = str(exc_info.value)
    assert secret not in err


# ---------------------------------------------------------------------------
# YouTubeClient.fetch_analytics — Analytics API v2 실연동 단위 테스트
# ---------------------------------------------------------------------------


def _analytics_report(rows: list[list] | None) -> FakeHttpResponse:
    """Analytics API v2 reports 응답(columnHeaders + rows) fake."""
    return FakeHttpResponse(
        status_code=200,
        body={
            "columnHeaders": [
                {"name": "views"},
                {"name": "likes"},
                {"name": "comments"},
                {"name": "averageViewDuration"},
            ],
            "rows": rows,
        },
    )


def test_youtube_fetch_analytics_parses_report():
    """exchange_token → reports 조회 → columnHeaders 순서로 메트릭을 매핑한다."""
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, body={"access_token": "tok"}),  # exchange_token
            _analytics_report([[999, 42, 7, 35.5]]),  # reports
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    result = client.fetch_analytics("video_123")

    assert result == {"views": 999, "likes": 42, "comments": 7, "averageViewDuration": 35.5}
    # reports 요청에 video 필터와 Bearer 토큰이 실렸는지 확인
    get_url, get_kwargs = http.get_calls[0]
    assert "youtubeanalytics.googleapis.com" in get_url
    assert get_kwargs["params"]["filters"] == "video==video_123"
    assert get_kwargs["headers"]["Authorization"] == "Bearer tok"


def test_youtube_fetch_analytics_no_rows_returns_empty():
    """rows가 없으면(업로드 직후 등) 빈 dict를 반환한다(상위에서 0 정규화)."""
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, body={"access_token": "tok"}),
            _analytics_report(None),  # rows 없음
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    assert client.fetch_analytics("video_new") == {}


def test_youtube_fetch_analytics_http_error_propagates():
    """reports 단계에서 HTTP 4xx/5xx면 PublishError(상태 코드)를 발생시킨다."""
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, body={"access_token": "tok"}),
            FakeHttpResponse(status_code=403, body={"error": "forbidden"}),
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    with pytest.raises(PublishError, match="403"):
        client.fetch_analytics("video_403")


def test_youtube_fetch_analytics_transport_error_does_not_leak_token():
    """reports 전송 오류 시 access_token이 PublishError에 노출되지 않는다.

    실제 구현은 access_token을 Authorization 헤더로 보내므로, TransportError의
    request.headers에 토큰이 박힌 상황을 시뮬레이션한다. `from None`으로 원본 예외
    체인을 끊는지(__cause__ is None)까지 단언해 redaction revert를 검출한다.
    """
    secret = "ANALYTICS_SECRET_TOKEN"
    fake_request = httpx.Request(
        "GET",
        "https://youtubeanalytics.googleapis.com/v2/reports",
        headers={"Authorization": f"Bearer {secret}"},
    )

    class _GetRaisingClient:
        def post(self, url, **kwargs):
            return FakeHttpResponse(status_code=200, body={"access_token": secret})

        def get(self, url, **kwargs):
            raise httpx.TransportError("연결 오류", request=fake_request)

    client = YouTubeClient(_yt_live_settings(), http=_GetRaisingClient())

    with pytest.raises(PublishError) as exc_info:
        client.fetch_analytics("video_x")

    assert secret not in str(exc_info.value)
    # from None 적용 확인 — 제거하면 __cause__에 토큰 보유 TransportError가 붙어 실패한다.
    assert exc_info.value.__cause__ is None


def test_youtube_fetch_analytics_malformed_columns_returns_empty():
    """columnHeaders 개수가 rows 값 개수와 불일치하면(부분 응답) 빈 dict를 반환한다."""
    http = FakeHttpClient(
        [
            FakeHttpResponse(status_code=200, body={"access_token": "tok"}),
            FakeHttpResponse(
                status_code=200,
                body={
                    # 헤더 2개인데 값은 4개 → zip이 조용히 자르는 상황
                    "columnHeaders": [{"name": "views"}, {"name": "likes"}],
                    "rows": [[10, 5, 2, 30.0]],
                },
            ),
        ]
    )
    client = YouTubeClient(_yt_live_settings(), http=http)

    assert client.fetch_analytics("video_partial") == {}


# ---------------------------------------------------------------------------
# 항목 12: Instagram PUBLISHED 상태 분기 / permalink-fallback URL / no-client fallback
# ---------------------------------------------------------------------------


def test_instagram_upload_published_status_proceeds():
    """폴링 중 PUBLISHED 상태가 반환되면 타임아웃 없이 게시 단계로 진행해야 한다.

    항목 12: PUBLISHED 분기(publishing.py: status == "PUBLISHED" → break)가 실제로
    동작하는지 검증한다. 이 테스트가 없으면 해당 분기가 삭제되어도 탐지되지 않는다.
    """
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    # PUBLISHED 상태를 즉시 반환해 폴링 루프가 break 되는지 확인
    fake_ig = FakeInstagramClient(
        creation_id="container_pub",
        media_id="media_pub",
        permalink="https://www.instagram.com/reel/media_pub/",
        poll_statuses=["PUBLISHED"],
    )
    publisher = Publisher(settings, ig_client=fake_ig, sleep=_no_sleep)

    result = publisher.upload_instagram(_video(), _meta())

    # 폴링이 1회(PUBLISHED에서 break) 후 publish까지 진행되어야 한다.
    assert len(fake_ig.poll_calls) == 1
    assert len(fake_ig.publish_calls) == 1
    assert result.platform == "instagram"
    assert result.external_id == "media_pub"


def test_instagram_upload_permalink_fallback_url():
    """fetch_permalink가 빈 문자열을 반환하면 fallback URL을 사용해야 한다.

    항목 12: permalink-fallback 분기(publishing.py: url = permalink or f"...")가
    실제로 동작하는지 검증한다. 이 분기가 없으면 빈 URL로 UploadResult가 만들어진다.
    """
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    # permalink=""를 반환해 fallback URL이 사용되는지 확인
    fake_ig = FakeInstagramClient(
        media_id="media_fallback",
        permalink="",  # 빈 문자열 → fallback URL 사용
        poll_statuses=["FINISHED"],
    )
    publisher = Publisher(settings, ig_client=fake_ig, sleep=_no_sleep)

    result = publisher.upload_instagram(_video(), _meta())

    # fallback URL 형식: https://www.instagram.com/reel/{media_id}/
    assert result.url == "https://www.instagram.com/reel/media_fallback/"
    assert result.external_id == "media_fallback"


def test_fetch_youtube_performance_no_client_creates_default(monkeypatch):
    """_fetch_youtube_performance가 yt_client 미주입 시 YouTubeClient를 생성해 호출 경로를 핀한다.

    fetch_analytics를 non-zero 더미 dict를 반환하도록 monkeypatch해 "클라이언트 생성 +
    호출 + 반환값 매핑" 경로가 실제로 동작함을 검증한다. stub의 0값만 단언하면
    try/finally 없이 결과를 만드는 우회 구현도 통과할 수 있어 vacuous해진다.
    """
    _FAKE_ANALYTICS = {
        "views": 777,
        "likes": 55,
        "comments": 9,
        "averageViewDuration": 42.0,
    }

    # YouTubeClient.fetch_analytics를 non-zero dict 반환으로 monkeypatch
    monkeypatch.setattr(YouTubeClient, "fetch_analytics", lambda self, vid: _FAKE_ANALYTICS)

    settings = _live_settings()
    # yt_client를 주입하지 않음 → _fetch_youtube_performance 내부에서 YouTubeClient 직접 생성
    publisher = Publisher(settings)
    upload = UploadResult(
        platform="youtube",
        external_id="video_no_client",
        url="https://youtube.com/shorts/video_no_client",
    )

    report = publisher.fetch_performance(upload)

    # monkeypatch한 non-zero 값이 올바르게 매핑됐는지 단언 — vacuous가 아님
    assert report.platform == "youtube"
    assert report.external_id == "video_no_client"
    assert report.views == 777
    assert report.likes == 55
    assert report.comments == 9
    assert report.avg_view_duration_sec == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# 리소스 누수 방지 — YouTubeClient/InstagramClient close() 회귀 방지
# ---------------------------------------------------------------------------


def test_upload_youtube_closes_self_created_yt_client(monkeypatch):
    """upload_youtube가 내부 생성한 YouTubeClient를 try/finally로 close()해야 한다.

    yt_client를 주입하지 않으면 Publisher가 직접 YouTubeClient를 생성하는데,
    이 인스턴스는 try/finally 안에서 정확히 1회 close()되어야 한다.
    이 테스트가 없으면 try/finally 블록을 삭제해도 ruff/테스트가 통과해버려
    httpx 커넥션 풀 누수가 재발할 수 있다.
    """
    import nutti.integrations.publishing as _pub_mod

    close_calls: list[str] = []

    class _TrackingYTClient:
        """exchange_token/upload_video/close 호출을 기록하는 가짜 클라이언트."""

        def exchange_token(self) -> str:
            return "fake_token"

        def upload_video(self, video: VideoAsset, meta: Metadata, access_token: str) -> str:
            return "fake_video_id"

        def close(self) -> None:
            close_calls.append("closed")

    _orig = _pub_mod.YouTubeClient

    def _fake_yt_constructor(settings):
        return _TrackingYTClient()

    monkeypatch.setattr(_pub_mod, "YouTubeClient", _fake_yt_constructor)

    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
    )
    publisher = Publisher(settings)  # yt_client 미주입 → 내부 생성
    publisher.upload_youtube(_video(), _meta())

    assert len(close_calls) == 1, (
        f"YouTubeClient.close()가 정확히 1회 호출되어야 합니다 (실제: {len(close_calls)}회)"
    )


def test_upload_youtube_does_not_close_injected_yt_client():
    """upload_youtube가 주입된 yt_client는 close()하지 않아야 한다.

    주입된 클라이언트의 수명은 호출자가 관리한다 — Publisher가 임의로 닫으면 안 된다.
    """
    settings = _live_settings(
        YOUTUBE_CLIENT_ID="cid",
        YOUTUBE_CLIENT_SECRET="csecret",
        YOUTUBE_REFRESH_TOKEN="rtoken",
    )

    class _TrackingInjected:
        def __init__(self):
            self.close_count = 0

        def exchange_token(self) -> str:
            return "fake_token"

        def upload_video(self, video: VideoAsset, meta: Metadata, access_token: str) -> str:
            return "fake_video_id"

        def close(self) -> None:
            self.close_count += 1

    injected = _TrackingInjected()
    publisher = Publisher(settings, yt_client=injected)
    publisher.upload_youtube(_video(), _meta())

    assert injected.close_count == 0, "주입된 yt_client는 Publisher가 close()해서는 안 됩니다"


def test_fetch_instagram_performance_no_client_creates_default(monkeypatch):
    """_fetch_instagram_performance가 ig_client 미주입 시 InstagramClient를 생성해 호출 경로를 핀한다.

    fetch_insights를 non-zero 더미 dict를 반환하도록 monkeypatch해 "클라이언트 생성 +
    호출 + 반환값 매핑(ms→초 변환 포함)" 경로가 실제로 동작함을 검증한다.
    """
    _FAKE_INSIGHTS = {
        "views": 400,
        "likes": 25,
        "comments": 4,
        "ig_reels_avg_watch_time": 21000,  # 21초 = 21000ms
    }

    monkeypatch.setattr(InstagramClient, "fetch_insights", lambda self, mid: _FAKE_INSIGHTS)

    settings = _live_settings()
    # ig_client를 주입하지 않음 → _fetch_instagram_performance 내부에서 InstagramClient 직접 생성
    publisher = Publisher(settings)
    upload = UploadResult(
        platform="instagram",
        external_id="media_no_client",
        url="https://www.instagram.com/reel/media_no_client/",
    )

    report = publisher.fetch_performance(upload)

    assert report.platform == "instagram"
    assert report.external_id == "media_no_client"
    assert report.views == 400
    assert report.likes == 25
    assert report.comments == 4
    # ms → 초 변환: 21000ms → 21.0초
    assert report.avg_view_duration_sec == pytest.approx(21.0)


def test_upload_instagram_closes_self_created_ig_client(monkeypatch):
    """upload_instagram이 내부 생성한 InstagramClient를 try/finally로 close()해야 한다.

    ig_client를 주입하지 않으면 Publisher가 직접 InstagramClient를 생성하는데,
    이 인스턴스는 try/finally 안에서 정확히 1회 close()되어야 한다.
    """
    import nutti.integrations.publishing as _pub_mod

    close_calls: list[str] = []

    class _TrackingIGClient:
        def create_container(self, video: VideoAsset, meta: Metadata) -> str:
            return "container_track"

        def poll_container(self, creation_id: str) -> str:
            return "FINISHED"

        def publish(self, creation_id: str) -> str:
            return "media_track"

        def fetch_permalink(self, media_id: str) -> str:
            return "https://www.instagram.com/reel/media_track/"

        def close(self) -> None:
            close_calls.append("closed")

    def _fake_ig_constructor(settings):
        return _TrackingIGClient()

    monkeypatch.setattr(_pub_mod, "InstagramClient", _fake_ig_constructor)

    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )
    publisher = Publisher(settings, sleep=_no_sleep)  # ig_client 미주입 → 내부 생성
    publisher.upload_instagram(_video(), _meta())

    assert len(close_calls) == 1, (
        f"InstagramClient.close()가 정확히 1회 호출되어야 합니다 (실제: {len(close_calls)}회)"
    )


def test_upload_instagram_does_not_close_injected_ig_client():
    """upload_instagram이 주입된 ig_client는 close()하지 않아야 한다."""
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )

    class _TrackingInjected:
        def __init__(self):
            self.close_count = 0

        def create_container(self, video: VideoAsset, meta: Metadata) -> str:
            return "container_inj"

        def poll_container(self, creation_id: str) -> str:
            return "FINISHED"

        def publish(self, creation_id: str) -> str:
            return "media_inj"

        def fetch_permalink(self, media_id: str) -> str:
            return "https://www.instagram.com/reel/media_inj/"

        def close(self) -> None:
            self.close_count += 1

    injected = _TrackingInjected()
    publisher = Publisher(settings, ig_client=injected, sleep=_no_sleep)
    publisher.upload_instagram(_video(), _meta())

    assert injected.close_count == 0, "주입된 ig_client는 Publisher가 close()해서는 안 됩니다"


def test_instagram_poll_container_error_mid_loop():
    """poll_container가 루프 중간에 PublishError를 발생시키면 즉시 전파되어야 한다.

    IN_PROGRESS → PublishError 순서로 poll이 호출될 때, Publisher 폴링 루프가
    예외를 잡지 않고 그대로 전파하는지 검증한다.
    FakeInstagramClient에 raise_on_poll 옵션을 추가해 이 경로를 커버한다.
    """
    settings = _live_settings(
        INSTAGRAM_ACCESS_TOKEN="ig_token",
        INSTAGRAM_ACCOUNT_ID="ig_acc_123",
    )

    call_count = 0

    class _PollErrorAfterFirstClient:
        """첫 번째 poll은 IN_PROGRESS, 두 번째는 PublishError를 발생시킨다."""

        def create_container(self, video: VideoAsset, meta: Metadata) -> str:
            return "container_err"

        def poll_container(self, creation_id: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "IN_PROGRESS"
            raise PublishError("poll_container 전송 오류 — 네트워크 단절")

        def publish(self, creation_id: str) -> str:  # pragma: no cover
            raise AssertionError("poll 오류 후 publish가 호출되면 안 됩니다")

        def fetch_permalink(self, media_id: str) -> str:  # pragma: no cover
            raise AssertionError("poll 오류 후 fetch_permalink가 호출되면 안 됩니다")

        def close(self) -> None:
            pass

    publisher = Publisher(
        settings,
        ig_client=_PollErrorAfterFirstClient(),
        sleep=_no_sleep,
    )

    with pytest.raises(PublishError, match="poll_container 전송 오류"):
        publisher.upload_instagram(_video(), _meta())

    # poll이 정확히 2회 호출(IN_PROGRESS + 예외) 후 루프 탈출
    assert call_count == 2
