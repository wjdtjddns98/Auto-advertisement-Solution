"""fal.ai Veo 3.1 백엔드 단위 테스트.

대상: FalVeoClient, VideoStudio veo_fal 분기, 비용 계산.
모든 테스트는 fake http 주입으로 **네트워크 없이** 동작한다.

섹션 구성:
  A. FalVeoClient — 제출·폴링·다운로드·오류·SSRF 방어
  B. VideoStudio veo_fal 분기 — 배선·dry_run·키 검증
  C. 비용 계산 — veo_fal lite/fast/standard 단가
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nutti.config import Settings
from nutti.integrations.video import VideoRenderError, VideoStudio, VideoTimeoutError
from nutti.integrations.video_veo_fal import FalVeoClient
from nutti.models import Script
from nutti.pipeline.cost import estimate_run_cost


# ─────────────────────────── 공통 헬퍼 ───────────────────────────


def _dry_settings(**overrides) -> Settings:
    """dry_run 환경 설정(네트워크/키 불요)."""
    base: dict = {"NUTTI_DRY_RUN": True}
    base.update(overrides)
    return Settings(**base)


def _live_settings(**overrides) -> Settings:
    """실 경로(non-dry_run) 설정."""
    base: dict = {"NUTTI_DRY_RUN": False, "GEMINI_API_KEY": "", "FAL_KEY": ""}
    base.update(overrides)
    return Settings(**base)


def _veo_fal_settings(**overrides) -> Settings:
    """veo_fal 백엔드 설정(실 경로, 키 채워짐)."""
    base: dict = {
        "NUTTI_DRY_RUN": False,
        "GEMINI_API_KEY": "test-gemini-key",
        "FAL_KEY": "test-fal-key",
        "NUTTI_VIDEO_BACKEND": "veo_fal",
        "NUTTI_VEO_FAL_POLL_INTERVAL_SEC": 1.0,
        "NUTTI_VEO_FAL_TIMEOUT_SEC": 30.0,
    }
    base.update(overrides)
    return Settings(**base)


def _no_sleep(_seconds):
    """폴링 대기 없이 즉시 반환하는 가짜 sleep."""
    return None


def _frame_file(tmp_path) -> str:
    """FalVeoClient._submit이 읽을 시작 프레임 파일을 만들어 경로 반환."""
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"FAKE-FRAME-BYTES")
    return str(frame)


def _script(
    topic: str = "강아지 간식",
    body: str = "누띠 간식은 건강해요!",
    beats: list[str] | None = None,
) -> Script:
    """테스트용 최소 대본."""
    return Script(topic=topic, body=body, beats=beats or [])


# ─────────────────────────── Fake HTTP ───────────────────────────


class _Resp:
    """httpx.Response 대역(status_code + headers + json + content)."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data=None,
        content: bytes = b"",
        json_exc: Exception | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.content = content
        self._json_exc = json_exc
        self.headers = dict(headers or {})

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json


class FakeVeoFalHttp:
    """FalVeoClient 주입용 fake HTTP 클라이언트.

    라우팅:
    - POST  → 제출 응답(post_response)
    - GET   /status suffix + queue.fal.run 호스트 → 폴링 큐(get_status_responses)
    - GET   결과 URL (큐 호스트 + /status 아님) → 결과 응답(get_result_response)
    - GET   fal.media URL → 다운로드 응답(download_response)
    """

    def __init__(
        self,
        *,
        post_response: _Resp | None = None,
        post_responses: list | None = None,
        post_exc: Exception | None = None,
        get_status_responses: list | None = None,
        get_result_response: _Resp | None = None,
        get_result_responses: list | None = None,
        download_response: _Resp | Exception | None = None,
    ):
        self.post_response = post_response or _Resp(json_data={"request_id": "veo-req-001"})
        # post_responses/get_result_responses: 주어지면 큐로 소비(429 재시도 테스트용).
        self.post_responses = list(post_responses or [])
        self.post_exc = post_exc
        self.get_status_responses = list(get_status_responses or [])
        self.get_result_response = get_result_response or _Resp(
            json_data={"video": {"url": "https://fal.media/fake/veo.mp4"}}
        )
        self.get_result_responses = list(get_result_responses or [])
        self.download_response = (
            download_response
            if download_response is not None
            else _Resp(content=b"FAKE-VEO-MP4-BYTES")
        )
        self.post_calls: list[tuple[str, dict | None]] = []
        self.post_headers: list[dict | None] = []
        self.status_calls: list[str] = []
        self.status_headers: list[dict | None] = []
        self.result_calls: list[str] = []
        self.result_headers: list[dict | None] = []
        self.download_calls: list[str] = []
        self.download_headers: list[dict | None] = []
        self.closed = False

    def post(self, url, *, headers=None, json=None):
        self.post_calls.append((url, json))
        self.post_headers.append(headers)
        if self.post_exc is not None:
            raise self.post_exc
        if self.post_responses:
            return self.post_responses.pop(0)
        return self.post_response

    def get(self, url, *, headers=None, follow_redirects=False):
        is_queue_host = "queue.fal.run" in url
        if is_queue_host and url.endswith("/status"):
            self.status_calls.append(url)
            self.status_headers.append(headers)
            item = self.get_status_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if is_queue_host:
            self.result_calls.append(url)
            self.result_headers.append(headers)
            if self.get_result_responses:
                return self.get_result_responses.pop(0)
            return self.get_result_response
        # 다운로드 URL (fal.media)
        self.download_calls.append(url)
        self.download_headers.append(headers)
        if isinstance(self.download_response, Exception):
            raise self.download_response
        return self.download_response

    def close(self):
        self.closed = True


def _fal_veo_client(tmp_path, fake, **setting_overrides) -> FalVeoClient:
    settings = _veo_fal_settings(NUTTI_MEDIA_DIR=str(tmp_path), **setting_overrides)
    return FalVeoClient(settings, http=fake, sleep=_no_sleep)


# ─────────────────────────── Fake 클라이언트 ───────────────────────────


class FakeFalVeoClient:
    """FalVeoClient 대역 — generate 호출 인자를 기록하고 결정적 경로를 반환한다."""

    def __init__(self, video_path: str = "data/fake/veo_fal.mp4"):
        self.video_path = video_path
        self.calls: list[tuple[str, str]] = []
        self.close_count = 0

    def generate(self, frame_path: str, prompt: str) -> str:
        self.calls.append((frame_path, prompt))
        return self.video_path

    def close(self):
        self.close_count += 1


class FakeNanoBananaClient:
    """NanoBananaClient 대역."""

    def __init__(self, frame_path: str = "data/fake/frame.jpg"):
        self.frame_path = frame_path
        self.calls: list[tuple[str, str | None]] = []
        self.close_count = 0

    def generate_frame(self, scene_prompt: str, *, reference_image_path: str | None = None) -> str:
        self.calls.append((scene_prompt, reference_image_path))
        return self.frame_path

    def close(self):
        self.close_count += 1


# ═══════════════════════════════════════════════════════════════════
# 섹션 A. FalVeoClient
# ═══════════════════════════════════════════════════════════════════


def test_fal_veo_client_generate_success_returns_path(tmp_path):
    """정상 흐름: 제출 → IN_QUEUE → COMPLETED → 다운로드 → 경로 반환."""
    fake = FakeVeoFalHttp(
        post_response=_Resp(json_data={"request_id": "veo-abc-001"}),
        get_status_responses=[
            _Resp(json_data={"status": "IN_QUEUE"}),
            _Resp(json_data={"status": "IN_PROGRESS"}),
            _Resp(json_data={"status": "COMPLETED"}),
        ],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/veo123.mp4"}}
        ),
        download_response=_Resp(content=b"FAKE-VEO-MP4"),
    )
    client = _fal_veo_client(tmp_path, fake)
    path = client.generate(_frame_file(tmp_path), "a dog mascot speaking Korean")

    assert Path(path).parent == tmp_path
    assert Path(path).name.startswith("veo_fal_")
    assert Path(path).suffix == ".mp4"
    assert Path(path).read_bytes() == b"FAKE-VEO-MP4"
    # 폴링 횟수: 3회(IN_QUEUE + IN_PROGRESS + COMPLETED)
    assert len(fake.status_calls) == 3


def test_fal_veo_client_status_result_use_app_id_not_full_model(tmp_path):
    """status/result URL은 앱 ID(앞 2세그먼트)만 사용해야 한다(Kling 405 사례와 동일).

    제출(POST)은 전체 모델 경로를 쓰고, status·result(GET)는 앱 ID만 쓴다.
    """
    fake = FakeVeoFalHttp(
        post_response=_Resp(json_data={"request_id": "veo-app-001"}),
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/v.mp4"}}
        ),
        download_response=_Resp(content=b"X"),
    )
    client = _fal_veo_client(
        tmp_path,
        fake,
        NUTTI_VEO_FAL_MODEL="fal-ai/veo3.1/lite/image-to-video",
    )
    client.generate(_frame_file(tmp_path), "prompt")

    # 제출 URL은 전체 모델 경로를 포함해야 한다.
    submit_url = fake.post_calls[0][0]
    assert submit_url.endswith("/fal-ai/veo3.1/lite/image-to-video")

    # status·result URL은 앱 ID(fal-ai/veo3.1) 기반이어야 한다.
    status_url = fake.status_calls[0]
    result_url = fake.result_calls[0]
    assert "/fal-ai/veo3.1/requests/veo-app-001/status" in status_url
    assert result_url.endswith("/fal-ai/veo3.1/requests/veo-app-001")
    assert "/lite/image-to-video/requests/" not in status_url
    assert "/lite/image-to-video/requests/" not in result_url


def test_fal_veo_client_submit_payload_contains_required_fields(tmp_path):
    """제출 페이로드에 필수 필드(prompt, image_url, generate_audio, aspect_ratio)가 있다."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        download_response=_Resp(content=b"X"),
    )
    client = _fal_veo_client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), "test prompt")

    assert len(fake.post_calls) == 1
    _, payload = fake.post_calls[0]
    assert payload is not None
    assert "prompt" in payload
    assert payload["prompt"] == "test prompt"
    assert "image_url" in payload
    # data URI 형태여야 한다.
    assert payload["image_url"].startswith("data:image/")
    assert "base64," in payload["image_url"]
    assert payload.get("generate_audio") is True
    assert payload.get("aspect_ratio") == "9:16"


def test_fal_veo_client_submit_payload_includes_negative_prompt(tmp_path):
    """제출 페이로드에 자막 억제 negative_prompt(설정값)가 실린다(화면 자막 방어)."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
    )
    client = _fal_veo_client(
        tmp_path, fake, NUTTI_VEO_FAL_NEGATIVE_PROMPT="no text, subtitles, korean text overlay"
    )

    client.generate(_frame_file(tmp_path), "test prompt")

    _, payload = fake.post_calls[0]
    assert payload["negative_prompt"] == "no text, subtitles, korean text overlay"


def test_fal_veo_client_submit_omits_empty_negative_prompt(tmp_path):
    """negative_prompt 설정이 비면 제출 페이로드에서 필드를 생략한다(불필요한 빈 값 미전송)."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
    )
    client = _fal_veo_client(tmp_path, fake, NUTTI_VEO_FAL_NEGATIVE_PROMPT="   ")

    client.generate(_frame_file(tmp_path), "test prompt")

    _, payload = fake.post_calls[0]
    assert "negative_prompt" not in payload


def test_fal_veo_client_submit_missing_request_id_raises(tmp_path):
    """제출 응답에 request_id가 없으면 VideoRenderError를 즉시 던진다."""
    fake = FakeVeoFalHttp(
        post_response=_Resp(json_data={"other": "field"}),
    )
    client = _fal_veo_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="request_id"):
        client.generate(_frame_file(tmp_path), "prompt")
    # 폴링까지 가지 않아야 한다.
    assert len(fake.status_calls) == 0


def test_fal_veo_client_error_status_raises_render_error(tmp_path):
    """status=ERROR면 VideoRenderError를 던진다."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "ERROR"})],
    )
    client = _fal_veo_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="status=ERROR"):
        client.generate(_frame_file(tmp_path), "prompt")


def test_fal_veo_client_timeout_raises_video_timeout_error(tmp_path):
    """폴링 타임아웃 시 VideoTimeoutError를 던진다(sleep 주입으로 빠르게)."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "IN_QUEUE"}) for _ in range(10)],
    )
    client = _fal_veo_client(
        tmp_path,
        fake,
        NUTTI_VEO_FAL_POLL_INTERVAL_SEC=1.0,
        NUTTI_VEO_FAL_TIMEOUT_SEC=2.0,
    )
    with pytest.raises(VideoTimeoutError) as exc_info:
        client.generate(_frame_file(tmp_path), "prompt")
    assert "폴링" in str(exc_info.value)


def test_fal_veo_client_transient_429_retries_and_succeeds(tmp_path):
    """상태 조회 429 → backoff 재시도 후 성공."""
    sleeps: list[float] = []
    fake = FakeVeoFalHttp(
        get_status_responses=[
            _Resp(status_code=429),
            _Resp(json_data={"status": "COMPLETED"}),
        ],
    )
    settings = _veo_fal_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    client = FalVeoClient(settings, http=fake, sleep=sleeps.append)
    path = client.generate(_frame_file(tmp_path), "prompt")
    assert Path(path).exists()
    # 429 1회 → 재시도 1회 = 총 2회 폴링
    assert len(fake.status_calls) == 2
    assert len(sleeps) >= 1
    assert sleeps[0] > 0


def test_fal_veo_client_transient_500_exhausted_raises(tmp_path):
    """연속 500이 재시도 한도를 초과하면 VideoRenderError로 전파된다."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(status_code=500) for _ in range(5)],
    )
    client = _fal_veo_client(tmp_path, fake)
    with pytest.raises(VideoRenderError) as exc_info:
        client.generate(_frame_file(tmp_path), "prompt")
    assert "500" in str(exc_info.value)
    # 최초 1회 + 재시도 3회 = 4회
    assert len(fake.status_calls) == 4


def test_fal_veo_client_submit_transient_429_retries_and_succeeds(tmp_path):
    """제출 429 → backoff 재시도 후 성공(생성 전 단계라 전체 파이프라인 보호)."""
    sleeps: list[float] = []
    fake = FakeVeoFalHttp(
        post_responses=[
            _Resp(status_code=429),
            _Resp(json_data={"request_id": "veo-submit-retry"}),
        ],
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
    )
    settings = _veo_fal_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    client = FalVeoClient(settings, http=fake, sleep=sleeps.append)
    path = client.generate(_frame_file(tmp_path), "prompt")
    assert Path(path).exists()
    assert len(fake.post_calls) == 2          # 429 1회 → 재시도 1회
    assert len(sleeps) >= 1 and sleeps[0] > 0  # 가짜 시계로 backoff 호출 확인


def test_fal_veo_client_result_transient_429_retries_and_succeeds(tmp_path):
    """결과 조회 429 → backoff 재시도 후 성공(생성 완료 후 과금 손실 방지)."""
    sleeps: list[float] = []
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_responses=[
            _Resp(status_code=429),
            _Resp(json_data={"video": {"url": "https://fal.media/clips/v.mp4"}}),
        ],
    )
    settings = _veo_fal_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    client = FalVeoClient(settings, http=fake, sleep=sleeps.append)
    path = client.generate(_frame_file(tmp_path), "prompt")
    assert Path(path).exists()
    assert len(fake.result_calls) == 2         # 429 1회 → 재시도 1회
    assert len(sleeps) >= 1 and sleeps[0] > 0


def test_fal_veo_client_error_messages_do_not_leak_response_keys(tmp_path):
    """redaction 계약: request_id/URL 누락 오류 메시지에 응답 키 목록을 노출하지 않는다."""
    # 제출 응답 키 누락.
    fake_submit = FakeVeoFalHttp(post_response=_Resp(json_data={"secret_field": "x"}))
    with pytest.raises(VideoRenderError) as exc:
        _fal_veo_client(tmp_path, fake_submit).generate(_frame_file(tmp_path), "prompt")
    assert "secret_field" not in str(exc.value) and "응답 키" not in str(exc.value)

    # 결과 응답 키 누락.
    fake_result = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(json_data={"unexpected_key": 1}),
    )
    with pytest.raises(VideoRenderError) as exc2:
        _fal_veo_client(tmp_path, fake_result).generate(_frame_file(tmp_path), "prompt")
    assert "unexpected_key" not in str(exc2.value) and "응답 키" not in str(exc2.value)


def test_fal_veo_client_result_missing_video_url_raises(tmp_path):
    """결과 응답에 video.url이 없으면 VideoRenderError를 던진다."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(json_data={"video": {}}),  # url 없음
    )
    client = _fal_veo_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="URL"):
        client.generate(_frame_file(tmp_path), "prompt")


def test_fal_veo_client_download_no_auth_header_to_cdn(tmp_path):
    """CDN(fal.media) 다운로드 요청에는 Authorization 헤더가 없다(자격증명 격리)."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/test.mp4"}}
        ),
        download_response=_Resp(content=b"MP4"),
    )
    client = _fal_veo_client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), "prompt")

    assert len(fake.download_calls) == 1
    dl_headers = fake.download_headers[0]
    if dl_headers:
        assert "authorization" not in {k.lower() for k in dl_headers}


def test_fal_veo_client_queue_requests_have_auth_header(tmp_path):
    """큐(queue.fal.run) 요청에는 Authorization: Key 헤더가 포함된다."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
    )
    client = _fal_veo_client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), "prompt")

    assert fake.post_headers
    assert "authorization" in {k.lower() for k in (fake.post_headers[0] or {})}
    assert fake.status_headers
    assert "authorization" in {k.lower() for k in (fake.status_headers[0] or {})}


def test_fal_veo_client_download_unsafe_url_raises(tmp_path):
    """다운로드 URL이 허용 외 호스트면 VideoRenderError(SSRF 방어)."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://evil.example.com/steal.mp4"}}
        ),
    )
    client = _fal_veo_client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.generate(_frame_file(tmp_path), "prompt")


def test_fal_veo_client_redirect_valid_location_succeeds(tmp_path):
    """다운로드 302 → 허용 호스트(fal.media) Location → 다운로드 성공."""
    redirect_served = {"done": False}

    class _RedirectHttp(FakeVeoFalHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            if "queue.fal.run" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            if not redirect_served["done"]:
                redirect_served["done"] = True
                return _Resp(
                    status_code=302,
                    headers={"location": "https://fal.media/clips/final.mp4"},
                )
            return _Resp(content=b"REDIRECTED-VEO-MP4")

    fake = _RedirectHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/initial.mp4"}}
        ),
    )
    client = _fal_veo_client(tmp_path, fake)
    path = client.generate(_frame_file(tmp_path), "prompt")
    assert Path(path).read_bytes() == b"REDIRECTED-VEO-MP4"


def test_fal_veo_client_redirect_unsafe_location_raises(tmp_path):
    """다운로드 302 → 허용 외 호스트 Location → VideoRenderError(SSRF 방어)."""

    class _EvilRedirectHttp(FakeVeoFalHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            if "queue.fal.run" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            return _Resp(
                status_code=302,
                headers={"location": "https://evil.example.com/steal.mp4"},
            )

    fake = _EvilRedirectHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/trigger.mp4"}}
        ),
    )
    client = _fal_veo_client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.generate(_frame_file(tmp_path), "prompt")


def test_fal_veo_client_chained_redirect_raises(tmp_path):
    """1차 리다이렉트 후 2차 리다이렉트는 거부된다(SSRF 체인 방지)."""
    first_served = {"done": False}

    class _ChainedHttp(FakeVeoFalHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            if "queue.fal.run" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            if not first_served["done"]:
                first_served["done"] = True
                return _Resp(
                    status_code=302,
                    headers={"location": "https://fal.media/clips/final.mp4"},
                )
            return _Resp(
                status_code=302,
                headers={"location": "https://fal.media/clips/again.mp4"},
            )

    fake = _ChainedHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/trigger.mp4"}}
        ),
    )
    client = _fal_veo_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="추가 리다이렉트"):
        client.generate(_frame_file(tmp_path), "prompt")


def test_fal_veo_client_redirect_missing_location_raises(tmp_path):
    """302 응답에 Location 헤더가 없으면 VideoRenderError."""

    class _NoLocHttp(FakeVeoFalHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            if "queue.fal.run" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            return _Resp(status_code=302, headers={})

    fake = _NoLocHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/noloc.mp4"}}
        ),
    )
    client = _fal_veo_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="Location"):
        client.generate(_frame_file(tmp_path), "prompt")


def test_fal_veo_client_download_empty_content_raises(tmp_path):
    """다운로드 응답 바이트가 비면 VideoRenderError."""
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        download_response=_Resp(status_code=200, content=b""),
    )
    client = _fal_veo_client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.generate(_frame_file(tmp_path), "prompt")


def test_fal_veo_client_poll_count_increments(tmp_path):
    """poll_count는 폴링 HTTP 시도 횟수를 정확히 기록한다."""
    fake = FakeVeoFalHttp(
        get_status_responses=[
            _Resp(json_data={"status": "IN_QUEUE"}),
            _Resp(json_data={"status": "COMPLETED"}),
        ],
    )
    client = _fal_veo_client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), "prompt")
    assert client.poll_count == 2


def test_fal_veo_client_rejects_nonpositive_interval(tmp_path):
    """veo_fal_poll_interval_sec ≤ 0이면 생성 시점에 ValueError."""
    for bad in (0.0, -1.0):
        settings = _veo_fal_settings(
            NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_VEO_FAL_POLL_INTERVAL_SEC=bad
        )
        with pytest.raises(ValueError, match="veo_fal_poll_interval_sec"):
            FalVeoClient(settings, http=FakeVeoFalHttp(), sleep=_no_sleep)


def test_fal_veo_client_rejects_nonpositive_timeout(tmp_path):
    """veo_fal_timeout_sec ≤ 0이면 생성 시점에 ValueError."""
    for bad in (0.0, -1.0):
        settings = _veo_fal_settings(
            NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_VEO_FAL_TIMEOUT_SEC=bad
        )
        with pytest.raises(ValueError, match="veo_fal_timeout_sec"):
            FalVeoClient(settings, http=FakeVeoFalHttp(), sleep=_no_sleep)


def test_fal_veo_client_close_closes_http(tmp_path):
    """close()가 주입된 HTTP 클라이언트를 닫는다."""
    fake = FakeVeoFalHttp()
    client = _fal_veo_client(tmp_path, fake)
    client.close()
    assert fake.closed is True


def test_fal_veo_client_missing_fal_key_creates_no_client():
    """FAL_KEY 없이 FalVeoClient를 생성하면 오류 없이 생성되나(키 검증은 VideoStudio가 담당),
    실제 generate 호출(실 httpx) 시 네트워크 가드가 막는다.

    이 테스트는 FalVeoClient 생성 자체(키 불필요)와 VideoStudio.validate_config(키 요구)를
    분리하는 계약을 검증한다 — fake http를 주입하면 키 없어도 동작한다.
    """
    settings = _veo_fal_settings(FAL_KEY="")
    # fake http 주입 → 실제 소켓 불필요, 생성 자체는 성공해야 한다.
    fake = FakeVeoFalHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
    )
    # _validate_model_id가 FAL_KEY를 검사하지 않으므로 생성 가능.
    client = FalVeoClient(settings, http=fake, sleep=_no_sleep)
    assert client is not None


# ═══════════════════════════════════════════════════════════════════
# 섹션 B. VideoStudio veo_fal 분기
# ═══════════════════════════════════════════════════════════════════


def test_videostudio_veo_fal_dry_run_no_external_call():
    """dry_run=True이면 veo_fal 백엔드가 아무 외부 호출 없이 결정적 더미 자산을 반환한다."""
    veo_fal = FakeFalVeoClient()
    settings = _dry_settings(NUTTI_VIDEO_BACKEND="veo_fal")
    studio = VideoStudio(settings, veo_fal_client=veo_fal)
    asset = studio.produce(_script())
    # dry_run에서는 fake 클라이언트가 호출되지 않는다.
    assert len(veo_fal.calls) == 0
    assert asset.final_url is not None
    assert asset.duration_sec > 0


def test_videostudio_veo_fal_dry_run_duration_is_clip_sec_times_beats():
    """dry_run에서 veo_fal duration은 _CLIP_SEC * len(beats)다(8×N 계산)."""
    settings = _dry_settings(NUTTI_VIDEO_BACKEND="veo_fal")
    studio = VideoStudio(settings)
    script = _script(beats=["b1", "b2", "b3"])
    asset = studio.produce(script)
    # veo_fal은 extend 없이 비트당 8초
    assert asset.duration_sec == pytest.approx(8.0 * 3)


def test_videostudio_veo_fal_routes_to_veo_fal_path(monkeypatch):
    """video_backend='veo_fal'이면 _produce_clips가 FalVeoClient 경로로 분기한다."""
    veo_fal = FakeFalVeoClient(video_path="data/fake/veo_fal.mp4")
    nano = FakeNanoBananaClient(frame_path="data/fake/frame.jpg")

    monkeypatch.setattr(VideoStudio, "_stitch", lambda self, clips, durations=None: clips[0])

    settings = _veo_fal_settings(NUTTI_VIDEO_BACKEND="veo_fal")
    studio = VideoStudio(settings, nano_client=nano, veo_fal_client=veo_fal)
    script = _script(body="간식 소개", beats=["비트1"])
    asset = studio.produce(script)

    # FalVeoClient.generate가 호출됐어야 한다.
    assert len(veo_fal.calls) == 1
    assert asset.script_id == script.id


def test_videostudio_veo_fal_each_beat_uses_same_frame(monkeypatch):
    """체이닝 폴백 경로: 끝 프레임 추출이 실패하면 모든 비트가 원본 마스코트 프레임을 공유한다.

    FakeFalVeoClient가 디스크에 없는 클립 경로를 반환하므로 _chain_frame이 None을 돌려
    (폴백) 모든 비트가 같은 frame_path로 generate된다. 체이닝 성공 경로는 아래 별도 테스트에서 검증.
    """
    veo_fal = FakeFalVeoClient()
    nano = FakeNanoBananaClient(frame_path="data/fake/shared_frame.jpg")

    monkeypatch.setattr(VideoStudio, "_stitch", lambda self, clips, durations=None: clips[0])

    settings = _veo_fal_settings(NUTTI_VIDEO_BACKEND="veo_fal")
    studio = VideoStudio(settings, nano_client=nano, veo_fal_client=veo_fal)
    script = _script(beats=["b1", "b2", "b3"])
    studio.produce(script)

    # 폴백: 모든 비트 generate 호출에 같은 원본 frame_path가 전달됐어야 한다.
    assert len(veo_fal.calls) == 3
    frame_paths = [call[0] for call in veo_fal.calls]
    assert all(p == "data/fake/shared_frame.jpg" for p in frame_paths)


def test_videostudio_veo_fal_chains_tail_frame_to_next_beat(monkeypatch):
    """체이닝 성공 경로: 각 클립의 끝 안정 프레임이 다음 비트 시작 프레임으로 쓰인다.

    _chain_frame이 실존 프레임(가드 통과)을 반환하면, 비트 1은 원본 마스코트 프레임에서
    시작하지만 비트 2·3은 직전 클립에서 추출한 chained 프레임으로 generate돼야 한다
    (비트 경계 자세 점프 완화의 핵심 동작). _chain_frame을 결정적으로 대체해 ffmpeg 없이 검증.
    """
    veo_fal = FakeFalVeoClient()
    nano = FakeNanoBananaClient(frame_path="data/fake/shared_frame.jpg")

    monkeypatch.setattr(VideoStudio, "_stitch", lambda self, clips, durations=None: clips[0])
    chained = iter(["data/fake/chain1.png", "data/fake/chain2.png"])
    monkeypatch.setattr(VideoStudio, "_chain_frame", lambda self, clip: next(chained))

    settings = _veo_fal_settings(NUTTI_VIDEO_BACKEND="veo_fal")
    studio = VideoStudio(settings, nano_client=nano, veo_fal_client=veo_fal)
    script = _script(beats=["b1", "b2", "b3"])
    studio.produce(script)

    frame_paths = [call[0] for call in veo_fal.calls]
    # 비트1=원본 마스코트, 비트2·3=직전 클립의 chained 프레임.
    assert frame_paths == [
        "data/fake/shared_frame.jpg",
        "data/fake/chain1.png",
        "data/fake/chain2.png",
    ]


def test_produce_veo_fal_cleans_up_completed_clips_on_midloop_failure(tmp_path):
    """비트 루프 중도 실패 시 이미 받은 클립 파일을 정리한다(수백 MB 누수 방지)."""
    created: list[Path] = []

    class _LeakyFalVeo:
        def __init__(self):
            self.n = 0

        def generate(self, frame_path, prompt):
            self.n += 1
            if self.n == 1:
                p = tmp_path / "veo_fal_leak1.mp4"
                p.write_bytes(b"CLIP1")
                created.append(p)
                return str(p)
            raise VideoRenderError("둘째 비트 생성 실패")

        def close(self):
            pass

    nano = FakeNanoBananaClient(frame_path="data/fake/frame.jpg")
    settings = _veo_fal_settings(NUTTI_VIDEO_BACKEND="veo_fal", NUTTI_MEDIA_DIR=str(tmp_path))
    studio = VideoStudio(settings, nano_client=nano, veo_fal_client=_LeakyFalVeo())
    script = _script(beats=["b1", "b2"])
    with pytest.raises(VideoRenderError):
        studio.produce(script)
    # 1번째 비트 클립이 정리돼 영구 잔존하지 않아야 한다.
    assert created and not created[0].exists()


def test_videostudio_veo_fal_duration_is_clip_sec_times_beats(monkeypatch):
    """veo_fal 백엔드 duration_sec = _CLIP_SEC * len(beats)(8×N, extend 없음)."""
    veo_fal = FakeFalVeoClient()
    nano = FakeNanoBananaClient()

    monkeypatch.setattr(VideoStudio, "_stitch", lambda self, clips, durations=None: clips[0])

    settings = _veo_fal_settings(NUTTI_VIDEO_BACKEND="veo_fal")
    studio = VideoStudio(settings, nano_client=nano, veo_fal_client=veo_fal)
    script = _script(beats=["b1", "b2", "b3"])
    asset = studio.produce(script)

    # 비트 3개 × 8초 = 24초. veo extend(8+7+7=22)와 다르다.
    assert asset.duration_sec == pytest.approx(24.0)
    assert asset.duration_sec != pytest.approx(22.0)  # veo extend 값이 아님


def test_videostudio_veo_fal_validate_config_does_not_require_gemini_key():
    """veo_fal은 GEMINI_API_KEY가 없어도 통과한다 — 프레임(Kontext)·영상 모두 fal(FAL_KEY).

    이미지 생성을 Gemini(NanoBanana)에서 fal Kontext로 옮기면서 veo_fal 파이프라인은
    Gemini 키가 완전히 불필요해졌다(결제처 fal 단일화). FAL_KEY만 있으면 검증을 통과해야 한다.
    """
    settings = _live_settings(NUTTI_VIDEO_BACKEND="veo_fal", GEMINI_API_KEY="", FAL_KEY="fk")
    studio = VideoStudio(settings)
    studio.validate_config()  # GEMINI 없이도 예외 없이 통과해야 한다


def test_videostudio_veo_fal_validate_config_missing_fal_key_raises():
    """veo_fal + dry_run=False + FAL_KEY 빈 값 → ValueError."""
    settings = _live_settings(NUTTI_VIDEO_BACKEND="veo_fal", GEMINI_API_KEY="gk", FAL_KEY="")
    studio = VideoStudio(settings)
    with pytest.raises(ValueError, match="FAL_KEY"):
        studio.validate_config()


def test_videostudio_veo_fal_validate_config_all_injected_skips_key_check():
    """nano_client + veo_fal_client 모두 주입 → 키 검사 없이 통과."""
    settings = _live_settings(NUTTI_VIDEO_BACKEND="veo_fal", GEMINI_API_KEY="", FAL_KEY="")
    studio = VideoStudio(
        settings,
        nano_client=FakeNanoBananaClient(),
        veo_fal_client=FakeFalVeoClient(),
    )
    # 예외 없이 통과해야 한다.
    studio.validate_config()


def test_videostudio_veo_fal_veo_fal_client_owned_is_closed(monkeypatch, tmp_path):
    """미주입 FalVeoClient(소유분)가 _produce_clips_veo_fal 종료 후 정확히 1회 닫힌다."""
    closed: list[bool] = []

    class _TrackingFalVeoClient(FakeFalVeoClient):
        def close(self):
            closed.append(True)

    created: list[_TrackingFalVeoClient] = []

    import nutti.integrations.video_veo_fal as vvf_module

    def _fake_cls(settings, *, sleep=None):
        c = _TrackingFalVeoClient(video_path=str(tmp_path / "veo_fal.mp4"))
        created.append(c)
        return c

    monkeypatch.setattr(vvf_module, "FalVeoClient", _fake_cls)
    monkeypatch.setattr(VideoStudio, "_stitch", lambda self, clips, durations=None: clips[0])

    settings = _veo_fal_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    nano = FakeNanoBananaClient()
    # veo_fal_client 미주입 → _produce_clips_veo_fal이 직접 생성·close
    studio = VideoStudio(settings, nano_client=nano)
    script = _script(beats=["b1"])
    studio.produce(script)

    assert len(created) == 1
    assert len(closed) == 1


# ═══════════════════════════════════════════════════════════════════
# 섹션 C. 비용 계산
# ═══════════════════════════════════════════════════════════════════


def _make_run_with_video(duration_sec: float, settings: Settings):
    """지정 duration을 가진 PipelineRun을 만든다(비용 계산 테스트용)."""
    from nutti.models import PipelineRun, VideoAsset

    script = Script(topic="테스트", body="테스트 대본")
    video = VideoAsset(
        script_id=script.id,
        frame_image_path="data/fake/frame.jpg",
        video_path="data/fake/video.mp4",
        final_url="data/fake/video.mp4",
        duration_sec=duration_sec,
    )
    return PipelineRun(topic="테스트", script=script, video=video)


def test_cost_veo_fal_lite_unit_price():
    """veo_fal lite 모델 → $0.05/초 단가."""
    settings = Settings(
        NUTTI_DRY_RUN=False,
        NUTTI_VIDEO_BACKEND="veo_fal",
        NUTTI_VEO_FAL_MODEL="fal-ai/veo3.1/lite/image-to-video",
    )
    run = _make_run_with_video(8.0, settings)
    cost = estimate_run_cost(run, settings)

    # 영상 라인 찾기
    video_items = [item for item in cost.items if "Veo(fal)" in item.label]
    assert len(video_items) == 1
    item = video_items[0]
    assert "Lite" in item.label
    assert item.usd == pytest.approx(0.05 * 8.0)


def test_cost_veo_fal_fast_unit_price():
    """veo_fal fast 모델 → $0.15/초 단가."""
    settings = Settings(
        NUTTI_DRY_RUN=False,
        NUTTI_VIDEO_BACKEND="veo_fal",
        NUTTI_VEO_FAL_MODEL="fal-ai/veo3.1/fast/image-to-video",
    )
    run = _make_run_with_video(8.0, settings)
    cost = estimate_run_cost(run, settings)

    video_items = [item for item in cost.items if "Veo(fal)" in item.label]
    assert len(video_items) == 1
    item = video_items[0]
    assert "Fast" in item.label
    assert item.usd == pytest.approx(0.15 * 8.0)


def test_cost_veo_fal_standard_unit_price():
    """veo_fal standard 모델 → $0.40/초 단가."""
    settings = Settings(
        NUTTI_DRY_RUN=False,
        NUTTI_VIDEO_BACKEND="veo_fal",
        NUTTI_VEO_FAL_MODEL="fal-ai/veo3.1/standard/image-to-video",
    )
    run = _make_run_with_video(10.0, settings)
    cost = estimate_run_cost(run, settings)

    video_items = [item for item in cost.items if "Veo(fal)" in item.label]
    assert len(video_items) == 1
    item = video_items[0]
    assert "Standard" in item.label
    assert item.usd == pytest.approx(0.40 * 10.0)


def test_cost_veo_fal_lite_4_beats_24sec():
    """veo_fal lite, 4비트(24초) → $0.05 × 24 = $1.20."""
    settings = Settings(
        NUTTI_DRY_RUN=False,
        NUTTI_VIDEO_BACKEND="veo_fal",
        NUTTI_VEO_FAL_MODEL="fal-ai/veo3.1/lite/image-to-video",
    )
    run = _make_run_with_video(24.0, settings)
    cost = estimate_run_cost(run, settings)

    video_items = [item for item in cost.items if "Veo(fal)" in item.label]
    assert len(video_items) == 1
    assert video_items[0].usd == pytest.approx(1.20)


def test_cost_veo_fal_dry_run_flag_preserved():
    """dry_run=True인 settings에서 estimate_run_cost는 dry_run=True를 반환한다."""
    settings = Settings(
        NUTTI_DRY_RUN=True,
        NUTTI_VIDEO_BACKEND="veo_fal",
        NUTTI_VEO_FAL_MODEL="fal-ai/veo3.1/lite/image-to-video",
    )
    run = _make_run_with_video(8.0, settings)
    cost = estimate_run_cost(run, settings)
    assert cost.dry_run is True
