"""FalKontextClient(fal.ai FLUX.1 Kontext 시작 프레임 생성) 단위 테스트.

fake HTTP 주입으로 네트워크 없이 fal 큐 흐름(제출→폴링→결과→다운로드)과
보안 계약(SSRF 방어·자격증명 격리·transient 재시도·redaction)을 검증한다.
test_video_veo_fal.py 패턴을 미러한다.
"""

from __future__ import annotations

import pytest

from nutti.config import Settings
from nutti.integrations.image_kontext import FalKontextClient
from nutti.integrations.video import VideoRenderError, VideoTimeoutError


def _no_sleep(_seconds: float) -> None:
    return None


def _kontext_settings(**overrides) -> Settings:
    base = {
        "NUTTI_DRY_RUN": False,
        "NUTTI_ENV": "test",
        "FAL_KEY": "test-fal-key",
        "NUTTI_KONTEXT_MODEL": "fal-ai/flux-pro/kontext",
    }
    base.update(overrides)
    return Settings(**base)


def _frame_file(tmp_path) -> str:
    """레퍼런스(마스코트) 이미지 파일을 만들어 경로 반환."""
    p = tmp_path / "mascot.png"
    p.write_bytes(b"FAKE-PNG-REFERENCE")
    return str(p)


class _Resp:
    """httpx.Response 대역(status_code + headers + json + content)."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data=None,
        content: bytes = b"",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.content = content
        self.headers = dict(headers or {})

    def json(self):
        return self._json


class FakeKontextHttp:
    """FalKontextClient 주입용 fake HTTP 클라이언트.

    라우팅:
    - POST → 제출(post_responses 큐 우선, 없으면 post_response)
    - GET queue.fal.run + /status → 폴링 큐(get_status_responses)
    - GET queue.fal.run(결과) → 결과(get_result_responses 큐 우선, 없으면 get_result_response)
    - GET 그 외(CDN) → 다운로드(download_response)
    """

    def __init__(
        self,
        *,
        post_response: _Resp | None = None,
        post_responses: list | None = None,
        get_status_responses: list | None = None,
        get_result_response: _Resp | None = None,
        get_result_responses: list | None = None,
        download_response=None,
    ):
        self.post_response = post_response or _Resp(json_data={"request_id": "kx-001"})
        self.post_responses = list(post_responses or [])
        self.get_status_responses = list(get_status_responses or [])
        self.get_result_response = get_result_response or _Resp(
            json_data={"images": [{"url": "https://v3.fal.media/files/out.png"}]}
        )
        self.get_result_responses = list(get_result_responses or [])
        self.download_response = (
            download_response
            if download_response is not None
            else _Resp(content=b"FAKE-PNG-BYTES", headers={"content-type": "image/png"})
        )
        self.post_calls: list[tuple[str, dict | None]] = []
        self.post_headers: list[dict | None] = []
        self.status_calls: list[str] = []
        self.result_calls: list[str] = []
        self.download_calls: list[str] = []
        self.download_headers: list[dict | None] = []
        self.closed = False

    def post(self, url, *, headers=None, json=None):
        self.post_calls.append((url, json))
        self.post_headers.append(headers)
        if self.post_responses:
            return self.post_responses.pop(0)
        return self.post_response

    def get(self, url, *, headers=None, follow_redirects=False):
        is_queue = "queue.fal.run" in url
        if is_queue and url.endswith("/status"):
            self.status_calls.append(url)
            item = self.get_status_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if is_queue:
            self.result_calls.append(url)
            if self.get_result_responses:
                return self.get_result_responses.pop(0)
            return self.get_result_response
        self.download_calls.append(url)
        self.download_headers.append(headers)
        if isinstance(self.download_response, Exception):
            raise self.download_response
        return self.download_response

    def close(self):
        self.closed = True


def _client(tmp_path, fake, **overrides) -> FalKontextClient:
    settings = _kontext_settings(NUTTI_MEDIA_DIR=str(tmp_path), **overrides)
    return FalKontextClient(settings, http=fake, sleep=_no_sleep)


# ─────────────────────────── 정상 흐름 ───────────────────────────


def test_generate_frame_success_returns_local_path(tmp_path):
    fake = FakeKontextHttp(
        post_response=_Resp(json_data={"request_id": "kx-abc"}),
        get_status_responses=[
            _Resp(json_data={"status": "IN_QUEUE"}),
            _Resp(json_data={"status": "COMPLETED"}),
        ],
        get_result_response=_Resp(
            json_data={"images": [{"url": "https://fal.media/files/frame.png"}]}
        ),
        download_response=_Resp(content=b"PNG", headers={"content-type": "image/png"}),
    )
    from pathlib import Path

    path = _client(tmp_path, fake).generate_frame("강아지 프레임", reference_image_path=_frame_file(tmp_path))
    assert Path(path).parent == tmp_path
    assert Path(path).name.startswith("frame_") and Path(path).suffix == ".png"
    assert Path(path).read_bytes() == b"PNG"


def test_reference_image_required(tmp_path):
    """레퍼런스 이미지(None)면 VideoRenderError — 캐릭터 일관성 보장 불가."""
    fake = FakeKontextHttp()
    with pytest.raises(VideoRenderError, match="레퍼런스"):
        _client(tmp_path, fake).generate_frame("프롬프트", reference_image_path=None)
    assert len(fake.post_calls) == 0  # 제출까지 가지 않는다


def test_submit_payload_has_reference_and_9_16(tmp_path):
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
    )
    _client(tmp_path, fake).generate_frame("씬 프롬프트", reference_image_path=_frame_file(tmp_path))
    _url, body = fake.post_calls[0]
    assert body["prompt"] == "씬 프롬프트"
    assert body["image_url"].startswith("data:image/png;base64,")  # 레퍼런스 base64
    assert body["aspect_ratio"] == "9:16"


def test_status_result_use_app_id(tmp_path):
    """status/result는 앱 ID(앞 2세그먼트)만 쓴다(fal 큐 405 방어)."""
    fake = FakeKontextHttp(get_status_responses=[_Resp(json_data={"status": "COMPLETED"})])
    _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))
    assert fake.post_calls[0][0].endswith("/fal-ai/flux-pro/kontext")
    assert "/fal-ai/flux-pro/requests/" in fake.status_calls[0]
    assert "/kontext/requests/" not in fake.status_calls[0]


# ─────────────────────────── 오류·재시도·redaction ───────────────────────────


def test_missing_request_id_raises_without_key_leak(tmp_path):
    fake = FakeKontextHttp(post_response=_Resp(json_data={"secret_field": "x"}))
    with pytest.raises(VideoRenderError) as exc:
        _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))
    assert "request_id" in str(exc.value)
    assert "secret_field" not in str(exc.value) and "응답 키" not in str(exc.value)


def test_missing_image_url_raises_without_key_leak(tmp_path):
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(json_data={"unexpected": 1}),
    )
    with pytest.raises(VideoRenderError) as exc:
        _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))
    assert "이미지 URL" in str(exc.value)
    assert "unexpected" not in str(exc.value) and "응답 키" not in str(exc.value)


def test_error_status_raises(tmp_path):
    fake = FakeKontextHttp(get_status_responses=[_Resp(json_data={"status": "ERROR"})])
    with pytest.raises(VideoRenderError, match="status=ERROR"):
        _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))


def test_timeout_raises(tmp_path):
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "IN_QUEUE"}) for _ in range(20)],
    )
    client = _client(
        tmp_path, fake, NUTTI_KONTEXT_POLL_INTERVAL_SEC=1.0, NUTTI_KONTEXT_TIMEOUT_SEC=2.0
    )
    with pytest.raises(VideoTimeoutError):
        client.generate_frame("p", reference_image_path=_frame_file(tmp_path))


def test_submit_transient_429_retries(tmp_path):
    sleeps: list[float] = []
    fake = FakeKontextHttp(
        post_responses=[_Resp(status_code=429), _Resp(json_data={"request_id": "kx-retry"})],
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
    )
    settings = _kontext_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    FalKontextClient(settings, http=fake, sleep=sleeps.append).generate_frame(
        "p", reference_image_path=_frame_file(tmp_path)
    )
    assert len(fake.post_calls) == 2 and sleeps and sleeps[0] > 0


def test_result_transient_429_retries(tmp_path):
    sleeps: list[float] = []
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_responses=[
            _Resp(status_code=429),
            _Resp(json_data={"images": [{"url": "https://fal.media/files/v.png"}]}),
        ],
    )
    settings = _kontext_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    FalKontextClient(settings, http=fake, sleep=sleeps.append).generate_frame(
        "p", reference_image_path=_frame_file(tmp_path)
    )
    assert len(fake.result_calls) == 2 and sleeps and sleeps[0] > 0


def test_status_transient_500_exhausted_raises(tmp_path):
    fake = FakeKontextHttp(get_status_responses=[_Resp(status_code=500) for _ in range(5)])
    with pytest.raises(VideoRenderError) as exc:
        _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))
    assert "500" in str(exc.value)


# ─────────────────────────── SSRF·자격증명 격리 ───────────────────────────


def test_download_allows_v3_fal_media(tmp_path):
    """출력 호스트 v3.fal.media를 허용한다(서브도메인 suffix 매칭)."""
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"images": [{"url": "https://v3.fal.media/files/o.png"}]}
        ),
    )
    from pathlib import Path

    path = _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))
    assert Path(path).exists()


def test_download_rejects_non_fal_host(tmp_path):
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(json_data={"images": [{"url": "https://evil.com/x.png"}]}),
    )
    with pytest.raises(VideoRenderError, match="호스트"):
        _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))
    assert len(fake.download_calls) == 0  # 검증 실패로 다운로드 시도 안 함


def test_download_rejects_non_https(tmp_path):
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(json_data={"images": [{"url": "http://fal.media/x.png"}]}),
    )
    with pytest.raises(VideoRenderError, match="scheme"):
        _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))


def test_download_no_auth_header_to_cdn(tmp_path):
    """CDN 다운로드 요청에는 Authorization(FAL_KEY) 헤더가 없다."""
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(json_data={"images": [{"url": "https://fal.media/x.png"}]}),
    )
    _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))
    assert fake.download_headers[0] is None  # get(uri, follow_redirects=False) — headers 미전달


def test_download_second_redirect_rejected(tmp_path):
    """허용 호스트 다운로드가 또 리다이렉트하면 거부(SSRF 체인 차단)."""
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(json_data={"images": [{"url": "https://fal.media/a.png"}]}),
        download_response=_Resp(status_code=302, headers={"location": "https://fal.media/b.png"}),
    )
    # 첫 다운로드(302)→Location 재검증 통과→두번째도 302면 거부.
    fake.download_response = _Resp(status_code=302, headers={"location": "https://fal.media/b.png"})
    with pytest.raises(VideoRenderError, match="리다이렉트"):
        _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))


def test_content_type_jpeg_saves_jpg(tmp_path):
    fake = FakeKontextHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(json_data={"images": [{"url": "https://fal.media/x"}]}),
        download_response=_Resp(content=b"JPG", headers={"content-type": "image/jpeg"}),
    )
    from pathlib import Path

    path = _client(tmp_path, fake).generate_frame("p", reference_image_path=_frame_file(tmp_path))
    assert Path(path).suffix == ".jpg"
