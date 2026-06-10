"""립싱크 영상 백엔드(Hedra Character-3) 단위 테스트.

대상: HedraLipsyncClient, LipsyncBackend, LipsyncPromptBuilder, 모듈 헬퍼.

모든 테스트는 fake 클라이언트/sleep 주입으로 **네트워크 없이** 동작한다.
ElevenLabs TTS는 fake로 주입하고, Hedra HTTP도 fake로 라우팅한다.

섹션 구성:
  A. SSRF/입력 검증 순수 함수
  B. HedraLipsyncClient — 자산 업로드·제출·폴링·다운로드·오류·SSRF·키 격리
  C. LipsyncPromptBuilder — 립싱크(말하는) 프롬프트
  D. LipsyncBackend.produce_beat_clips — 비트 루프·total_sec·정리·소유권
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nutti.integrations.video_lipsync as lipsync_module
from nutti.config import Settings
from nutti.integrations.video import VideoRenderError, VideoTimeoutError
from nutti.integrations.video_lipsync import (
    HedraLipsyncClient,
    LipsyncBackend,
    LipsyncPromptBuilder,
    _hedra_allowed_hosts,
    _parse_extra_hosts,
    _validate_hedra_download_url,
    _validate_resource_id,
)


# ─────────────────────────── 공통 헬퍼 ───────────────────────────


def _lipsync_settings(**overrides) -> Settings:
    """립싱크 백엔드 설정(실 경로, 키 채워짐, 폴링 빠르게)."""
    base: dict = {
        "NUTTI_DRY_RUN": False,
        "HEDRA_API_KEY": "test-hedra-key",
        "ELEVENLABS_API_KEY": "test-eleven-key",
        "NUTTI_VIDEO_BACKEND": "lipsync",
        "NUTTI_LIPSYNC_POLL_INTERVAL_SEC": 1.0,
        "NUTTI_LIPSYNC_TIMEOUT_SEC": 30.0,
    }
    base.update(overrides)
    return Settings(**base)


def _no_sleep(_seconds):
    return None


def _frame_file(tmp_path) -> str:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"FAKE-FRAME-BYTES")
    return str(frame)


def _voice_file(tmp_path, name: str = "voice.wav") -> str:
    p = tmp_path / name
    p.write_bytes(b"RIFFvoice-bytes")
    return str(p)


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


class FakeHedraHttp:
    """HedraLipsyncClient 주입용 fake HTTP 클라이언트.

    POST 라우팅:
    - `/assets/{id}/upload` → 업로드 응답(upload_response)
    - `/assets`            → 자산 생성 응답(asset_create_responses 큐, 순서대로)
    - `/generations`       → 제출 응답(submit_response)
    GET 라우팅:
    - `/status` 포함 URL   → 상태 폴링 큐(status_responses)
    - 그 외(다운로드 URL)   → 다운로드 응답(download_response)

    각 요청 유형별 헤더를 기록해 자격증명 격리를 검증한다.
    """

    def __init__(
        self,
        *,
        asset_create_responses: list | None = None,
        upload_response: _Resp | None = None,
        submit_response: _Resp | None = None,
        status_responses: list | None = None,
        download_response: _Resp | Exception | None = None,
    ):
        self.asset_create_responses = list(
            asset_create_responses
            or [
                _Resp(json_data={"id": "asset-img-001"}),
                _Resp(json_data={"id": "asset-aud-001"}),
            ]
        )
        self.upload_response = upload_response or _Resp(status_code=200, json_data={"ok": True})
        self.submit_response = submit_response or _Resp(json_data={"id": "gen-001"})
        self.status_responses = list(status_responses or [])
        self.download_response = (
            download_response
            if download_response is not None
            else _Resp(content=b"FAKE-LIPSYNC-MP4")
        )
        self.asset_create_calls: list[tuple[str, dict | None]] = []
        self.asset_create_headers: list[dict | None] = []
        self.upload_calls: list[str] = []
        self.upload_headers: list[dict | None] = []
        self.submit_calls: list[tuple[str, dict | None]] = []
        self.submit_headers: list[dict | None] = []
        self.status_calls: list[str] = []
        self.status_headers: list[dict | None] = []
        self.download_calls: list[str] = []
        self.download_headers: list[dict | None] = []
        self.closed = False

    def post(self, url, *, headers=None, json=None, files=None):
        if url.endswith("/upload"):
            self.upload_calls.append(url)
            self.upload_headers.append(headers)
            return self.upload_response
        if url.endswith("/generations"):
            self.submit_calls.append((url, json))
            self.submit_headers.append(headers)
            return self.submit_response
        # 그 외 POST는 자산 생성(/assets)
        self.asset_create_calls.append((url, json))
        self.asset_create_headers.append(headers)
        item = self.asset_create_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, *, headers=None, follow_redirects=False):
        if "/status" in url:
            self.status_calls.append(url)
            self.status_headers.append(headers)
            item = self.status_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        # 다운로드 URL
        self.download_calls.append(url)
        self.download_headers.append(headers)
        if isinstance(self.download_response, Exception):
            raise self.download_response
        return self.download_response

    def close(self):
        self.closed = True


def _client(tmp_path, fake, **overrides) -> HedraLipsyncClient:
    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path), **overrides)
    return HedraLipsyncClient(settings, http=fake, sleep=_no_sleep)


def _complete_status(url: str = "https://api.hedra.com/files/out.mp4") -> _Resp:
    return _Resp(json_data={"status": "complete", "download_url": url})


# ═══════════════════════════════════════════════════════════════════
# 섹션 A. SSRF / 입력 검증 순수 함수
# ═══════════════════════════════════════════════════════════════════


def test_validate_hedra_download_url_allows_api_host():
    """api.hedra.com HTTPS URL은 통과한다."""
    _validate_hedra_download_url("https://api.hedra.com/files/out.mp4")


def test_validate_hedra_download_url_allows_hedra_subdomains():
    """확인된 Hedra 자체 도메인(서브도메인 포함)은 허용한다."""
    _validate_hedra_download_url("https://cdn.hedra.com/out.mp4")
    _validate_hedra_download_url("https://api.hedra.com/files/out.mp4")


def test_validate_hedra_download_url_rejects_broad_cdn_tlds():
    """TLD 단위 S3/CloudFront 허용은 공격자 버킷까지 통과시키므로 거부한다(SSRF 방어).

    키 확보 전 실제 CDN 서브도메인이 미확인이므로, 안전 폴백은 미확인 호스트 차단이다.
    (라이브로 정확한 CDN 호스트를 관측하면 그 호스트만 _HEDRA_SAFE_HOSTS에 추가한다.)
    """
    with pytest.raises(VideoRenderError):
        _validate_hedra_download_url("https://attacker-bucket.s3.amazonaws.com/keylog.mp4")
    with pytest.raises(VideoRenderError):
        _validate_hedra_download_url("https://attacker.cloudfront.net/steal.mp4")


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://api.hedra.com/out.mp4",       # http → 거부
        "https://evil.example.com/out.mp4",   # 타 호스트
        "ftp://api.hedra.com/out.mp4",        # ftp scheme
        "https://nothedra.com/out.mp4",       # hedra.com으로 안 끝남
        "https://hedra.com.evil.com/out.mp4", # 접미사 위장
        "",
    ],
)
def test_validate_hedra_download_url_rejects_unsafe(bad_url):
    """허용 외 scheme·호스트는 VideoRenderError."""
    with pytest.raises(VideoRenderError):
        _validate_hedra_download_url(bad_url)


def test_validate_hedra_download_url_accepts_configured_extra_host():
    """설정으로 추가된 정확 호스트는 그 집합을 넘기면 통과한다(운영 CDN 호스트 활성화)."""
    hosts = _hedra_allowed_hosts(
        _lipsync_settings(NUTTI_HEDRA_DOWNLOAD_HOSTS="d1234abcd.cloudfront.net")
    )
    # 추가 호스트와 그 서브도메인은 통과, 기본 안전 호스트도 여전히 통과.
    _validate_hedra_download_url("https://d1234abcd.cloudfront.net/out.mp4", hosts)
    _validate_hedra_download_url("https://api.hedra.com/files/out.mp4", hosts)
    # 추가하지 않은 다른 cloudfront 호스트는 여전히 거부(TLD 와일드카드 아님).
    with pytest.raises(VideoRenderError):
        _validate_hedra_download_url("https://attacker.cloudfront.net/steal.mp4", hosts)


# ─────────────────────────── 추가 허용 호스트 파서 ───────────────────────────


def test_parse_extra_hosts_normalizes_and_splits():
    """콤마 구분 정확 호스트명을 소문자로 정규화해 집합으로 만든다."""
    parsed = _parse_extra_hosts("Files.Hedra.com, d1234.cloudfront.net ")
    assert parsed == frozenset({"files.hedra.com", "d1234.cloudfront.net"})


def test_parse_extra_hosts_empty_is_empty():
    """빈 문자열·공백만 있으면 빈 집합."""
    assert _parse_extra_hosts("") == frozenset()
    assert _parse_extra_hosts("  ,  , ") == frozenset()


@pytest.mark.parametrize(
    "raw",
    [
        "*.amazonaws.com",           # 와일드카드 TLD → SSRF 재유입, 거부
        "*.cloudfront.net",          # 와일드카드 → 거부
        "https://files.hedra.com",   # 스킴 포함 → 거부
        "files.hedra.com/path",      # 경로 포함 → 거부
        "files.hedra.com:8443",      # 포트 포함 → 거부
        "files hedra com",           # 공백 → 거부
        ".hedra.com",                # 선행 점 → 거부
        "hedra..com",                # 빈 라벨 → 거부
    ],
)
def test_parse_extra_hosts_rejects_unsafe_entries(raw):
    """와일드카드/스킴/경로/포트/공백이 든 항목은 조용히 버린다(SSRF 재유입 차단)."""
    assert _parse_extra_hosts(raw) == frozenset()


def test_client_download_from_configured_cdn_host_succeeds(tmp_path):
    """설정으로 추가한 CDN 호스트의 download_url은 실제로 다운로드까지 성공한다.

    이 테스트가 핵심 회귀 가드다 — 추가 호스트 설정이 없으면(원래 코드처럼) 모든 CDN
    download_url이 '호스트 불허'로 거부돼 립싱크 백엔드가 운영에서 동작하지 않는다.
    """
    cdn = "https://d1234abcd.cloudfront.net/files/out.mp4"
    fake = FakeHedraHttp(
        status_responses=[_complete_status(cdn)],
        download_response=_Resp(content=b"FAKE-LIPSYNC-MP4"),
    )
    client = _client(tmp_path, fake, NUTTI_HEDRA_DOWNLOAD_HOSTS="d1234abcd.cloudfront.net")
    out = client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert Path(out).exists()
    # CDN 다운로드 요청에는 자격증명(X-API-Key)을 붙이지 않는다(키 격리 유지).
    assert fake.download_headers[0] is None


def test_client_download_unconfigured_cdn_host_rejected(tmp_path):
    """설정에 없는 CDN 호스트는 기본 안전 정책대로 거부된다(SSRF 방어 유지)."""
    cdn = "https://random-bucket.s3.amazonaws.com/out.mp4"
    fake = FakeHedraHttp(status_responses=[_complete_status(cdn)])
    client = _client(tmp_path, fake)  # 추가 호스트 미설정
    with pytest.raises(VideoRenderError, match="호스트 불허"):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")


def test_validate_resource_id_allows_valid():
    """영숫자·`-`·`_`만 있는 id는 그대로 반환한다."""
    assert _validate_resource_id("gen-abc_123-XYZ", what="생성") == "gen-abc_123-XYZ"


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "   ",
        "../etc/passwd",
        "a" * 129,
        "gen?inject=1",
        "gen/path/../evil",
    ],
)
def test_validate_resource_id_rejects_malformed(bad_id):
    """허용 외 문자 / 빈 값 / 과길이는 VideoRenderError."""
    with pytest.raises(VideoRenderError):
        _validate_resource_id(bad_id, what="생성")


# ═══════════════════════════════════════════════════════════════════
# 섹션 B. HedraLipsyncClient
# ═══════════════════════════════════════════════════════════════════


def test_client_generate_success_returns_path(tmp_path):
    """정상 흐름: 자산 2개 업로드 → 제출 → queued → processing → complete → 다운로드."""
    fake = FakeHedraHttp(
        status_responses=[
            _Resp(json_data={"status": "queued"}),
            _Resp(json_data={"status": "processing"}),
            _complete_status("https://api.hedra.com/files/v1.mp4"),
        ],
        download_response=_Resp(content=b"FAKE-LIPSYNC-MP4"),
    )
    client = _client(tmp_path, fake)
    path = client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "a talking dog")
    assert Path(path).parent == tmp_path
    assert Path(path).name.startswith("lipsync_")
    assert Path(path).suffix == ".mp4"
    assert Path(path).read_bytes() == b"FAKE-LIPSYNC-MP4"
    # 자산 2개(이미지·오디오) 생성 + 업로드 각 1회
    assert len(fake.asset_create_calls) == 2
    assert len(fake.upload_calls) == 2
    # 폴링 3회
    assert len(fake.status_calls) == 3


def test_client_submit_body_wires_asset_ids(tmp_path):
    """제출 본문이 업로드한 이미지·오디오 asset_id를 start_keyframe_id·audio_id로 배선한다."""
    fake = FakeHedraHttp(
        asset_create_responses=[
            _Resp(json_data={"id": "img-XYZ"}),
            _Resp(json_data={"id": "aud-XYZ"}),
        ],
        status_responses=[_complete_status()],
    )
    client = _client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    submit_body = fake.submit_calls[0][1]
    assert submit_body["type"] == "video"
    assert submit_body["ai_model_id"] == "character-3"
    assert submit_body["start_keyframe_id"] == "img-XYZ"
    assert submit_body["audio_id"] == "aud-XYZ"
    assert submit_body["generated_video_inputs"]["aspect_ratio"] == "9:16"


def test_client_status_url_uses_generation_id(tmp_path):
    """폴링 URL은 검증된 generation_id로 직접 구성된다(/generations/{id}/status)."""
    fake = FakeHedraHttp(
        submit_response=_Resp(json_data={"id": "gen-poll-001"}),
        status_responses=[_complete_status()],
    )
    client = _client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert fake.status_calls[0].endswith("/generations/gen-poll-001/status")


def test_client_error_status_raises_render_error(tmp_path):
    """status=error면 VideoRenderError를 던진다(error_message 원문 미노출)."""
    fake = FakeHedraHttp(
        status_responses=[
            _Resp(json_data={"status": "error", "error_message": "secret-internal-detail"})
        ],
    )
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError) as exc_info:
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert "status=error" in str(exc_info.value)
    assert "secret-internal-detail" not in str(exc_info.value)


def test_client_timeout_raises_video_timeout_error(tmp_path):
    """폴링 타임아웃 시 VideoTimeoutError를 던진다(sleep 주입으로 빠르게)."""
    fake = FakeHedraHttp(
        status_responses=[_Resp(json_data={"status": "processing"}) for _ in range(10)],
    )
    client = _client(
        tmp_path,
        fake,
        NUTTI_LIPSYNC_POLL_INTERVAL_SEC=1.0,
        NUTTI_LIPSYNC_TIMEOUT_SEC=2.0,
    )
    with pytest.raises(VideoTimeoutError) as exc_info:
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert "폴링" in str(exc_info.value)


def test_client_transient_429_retries_and_succeeds(tmp_path):
    """상태 조회 429 → backoff 재시도 후 성공."""
    sleeps: list[float] = []
    fake = FakeHedraHttp(
        status_responses=[_Resp(status_code=429), _complete_status()],
    )
    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    client = HedraLipsyncClient(settings, http=fake, sleep=sleeps.append)
    path = client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert Path(path).exists()
    assert len(fake.status_calls) == 2
    assert len(sleeps) >= 1
    assert sleeps[0] > 0


def test_client_transient_500_exhausted_raises(tmp_path):
    """연속 500이 재시도 한도를 초과하면 VideoRenderError로 전파된다."""
    fake = FakeHedraHttp(
        status_responses=[_Resp(status_code=500) for _ in range(5)],
    )
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError) as exc_info:
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert "500" in str(exc_info.value)
    # 최초 1회 + 재시도 3회 = 4회
    assert len(fake.status_calls) == 4


def test_client_asset_create_missing_id_raises(tmp_path):
    """자산 생성 응답에 id가 없으면 VideoRenderError를 즉시 던진다(제출까지 안 감)."""
    fake = FakeHedraHttp(
        asset_create_responses=[_Resp(json_data={"other": "field"})],
    )
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="id"):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert len(fake.submit_calls) == 0


def test_client_submit_missing_generation_id_raises(tmp_path):
    """제출 응답에 generation id가 없으면 VideoRenderError(폴링까지 안 감)."""
    fake = FakeHedraHttp(
        submit_response=_Resp(json_data={"foo": "bar"}),
    )
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="generation id"):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert len(fake.status_calls) == 0


def test_client_complete_missing_url_raises(tmp_path):
    """complete인데 download_url·url이 둘 다 없으면 VideoRenderError."""
    fake = FakeHedraHttp(
        status_responses=[_Resp(json_data={"status": "complete"})],
    )
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="URL"):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")


def test_client_complete_falls_back_to_url_field(tmp_path):
    """download_url이 없으면 url 필드로 폴백한다."""
    fake = FakeHedraHttp(
        status_responses=[_Resp(json_data={"status": "complete", "url": "https://api.hedra.com/f.mp4"})],
    )
    client = _client(tmp_path, fake)
    path = client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert Path(path).exists()


def test_client_download_unsafe_host_raises(tmp_path):
    """complete 응답의 download_url이 허용 외 호스트면 VideoRenderError(SSRF 방어)."""
    fake = FakeHedraHttp(
        status_responses=[_complete_status("https://evil.example.com/steal.mp4")],
    )
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="호스트 불허"):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")


def test_client_download_empty_content_raises(tmp_path):
    """다운로드 응답 바이트가 비면 VideoRenderError."""
    fake = FakeHedraHttp(
        status_responses=[_complete_status()],
        download_response=_Resp(status_code=200, content=b""),
    )
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")


def test_client_queue_requests_have_api_key_header(tmp_path):
    """Hedra API 요청(자산 생성·제출·상태)에는 X-API-Key 헤더가 포함된다."""
    fake = FakeHedraHttp(status_responses=[_complete_status()])
    client = _client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert "x-api-key" in {k.lower() for k in (fake.asset_create_headers[0] or {})}
    assert "x-api-key" in {k.lower() for k in (fake.submit_headers[0] or {})}
    assert "x-api-key" in {k.lower() for k in (fake.status_headers[0] or {})}


def test_client_upload_has_api_key_but_no_json_content_type(tmp_path):
    """업로드 요청은 X-API-Key는 있고 JSON Content-Type은 없다(multipart boundary 보존)."""
    fake = FakeHedraHttp(status_responses=[_complete_status()])
    client = _client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    up_headers = fake.upload_headers[0] or {}
    lowered = {k.lower() for k in up_headers}
    assert "x-api-key" in lowered
    assert "content-type" not in lowered


def test_client_download_from_cdn_has_no_api_key(tmp_path):
    """다운로드 URL이 api.hedra.com이 아니면(CDN) X-API-Key를 붙이지 않는다(키 격리).

    cdn.hedra.com은 허용 호스트(hedra.com 서브도메인)지만 API 호스트는 아니므로,
    구현은 키를 빼고(headers=None) 받는다 — CDN/중간자 키 유출 방지.
    """
    fake = FakeHedraHttp(
        status_responses=[_complete_status("https://cdn.hedra.com/out.mp4")],
    )
    client = _client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    dl_headers = fake.download_headers[0]
    # CDN 요청에는 X-API-Key가 없어야 한다. 구현은 CDN일 때 headers=None을 넘기므로
    # None도 명시적으로 허용하되, 헤더 dict이 오면 그 안에 키가 없음을 강제한다.
    # (if dl_headers: 가드로 감싸면 None일 때 단언이 통째로 스킵돼 공백 통과한다 — 금지.)
    assert dl_headers is None or "x-api-key" not in {k.lower() for k in dl_headers}


def test_client_download_from_api_host_keeps_api_key(tmp_path):
    """다운로드 URL이 api.hedra.com 직링크면 X-API-Key를 유지한다."""
    fake = FakeHedraHttp(
        status_responses=[_complete_status("https://api.hedra.com/files/out.mp4")],
    )
    client = _client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    dl_headers = fake.download_headers[0] or {}
    assert "x-api-key" in {k.lower() for k in dl_headers}


def test_client_redirect_unsafe_location_raises(tmp_path):
    """다운로드 302 → 허용 외 호스트 Location → VideoRenderError(SSRF 체인 방어)."""

    class _EvilRedirect(FakeHedraHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            if "/status" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            self.download_calls.append(url)
            self.download_headers.append(headers)
            return _Resp(status_code=302, headers={"location": "https://evil.example.com/x.mp4"})

    fake = _EvilRedirect(status_responses=[_complete_status()])
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")


def test_client_redirect_missing_location_raises(tmp_path):
    """302 응답에 Location 헤더가 없으면 VideoRenderError."""

    class _NoLoc(FakeHedraHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            if "/status" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            self.download_calls.append(url)
            self.download_headers.append(headers)
            return _Resp(status_code=302, headers={})

    fake = _NoLoc(status_responses=[_complete_status()])
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="Location"):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")


def test_client_redirect_chained_raises(tmp_path):
    """1차 리다이렉트 후 2차 리다이렉트는 거부된다(SSRF 체인 방지)."""
    first = {"done": False}

    class _Chained(FakeHedraHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            if "/status" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            self.download_calls.append(url)
            self.download_headers.append(headers)
            if not first["done"]:
                first["done"] = True
                return _Resp(status_code=302, headers={"location": "https://api.hedra.com/a.mp4"})
            return _Resp(status_code=302, headers={"location": "https://api.hedra.com/b.mp4"})

    fake = _Chained(status_responses=[_complete_status()])
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="추가 리다이렉트"):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")


def test_client_transport_error_raises_render_error(tmp_path):
    """전송 오류(ConnectionError)도 VideoRenderError로 승격된다."""
    fake = FakeHedraHttp(
        status_responses=[_complete_status()],
        download_response=ConnectionError("boom"),
    )
    client = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")


def test_client_rejects_nonpositive_interval(tmp_path):
    """lipsync_poll_interval_sec ≤ 0이면 생성 시점에 ValueError."""
    for bad in (0.0, -1.0):
        settings = _lipsync_settings(
            NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_LIPSYNC_POLL_INTERVAL_SEC=bad
        )
        with pytest.raises(ValueError, match="lipsync_poll_interval_sec"):
            HedraLipsyncClient(settings, http=FakeHedraHttp(), sleep=_no_sleep)


def test_client_rejects_nonpositive_timeout(tmp_path):
    """lipsync_timeout_sec ≤ 0이면 생성 시점에 ValueError."""
    for bad in (0.0, -1.0):
        settings = _lipsync_settings(
            NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_LIPSYNC_TIMEOUT_SEC=bad
        )
        with pytest.raises(ValueError, match="lipsync_timeout_sec"):
            HedraLipsyncClient(settings, http=FakeHedraHttp(), sleep=_no_sleep)


@pytest.mark.parametrize(
    "bad_model",
    ["model?q=inject", "model with spaces", "model:method", "model#frag", "", "   "],
)
def test_client_rejects_malformed_model(tmp_path, bad_model):
    """비정상 lipsync_model은 생성 시점에 VideoRenderError(본문 인젝션 방어)."""
    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_LIPSYNC_MODEL=bad_model)
    with pytest.raises(VideoRenderError, match="NUTTI_LIPSYNC_MODEL"):
        HedraLipsyncClient(settings, http=FakeHedraHttp(), sleep=_no_sleep)


def test_client_close_closes_http(tmp_path):
    """close()가 주입된 HTTP 클라이언트를 닫는다."""
    fake = FakeHedraHttp()
    client = _client(tmp_path, fake)
    client.close()
    assert fake.closed is True


def test_client_poll_count_increments(tmp_path):
    """poll_count는 폴링 HTTP 시도 횟수를 정확히 기록한다."""
    fake = FakeHedraHttp(
        status_responses=[
            _Resp(json_data={"status": "queued"}),
            _complete_status(),
        ],
    )
    client = _client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), _voice_file(tmp_path), "prompt")
    assert client.poll_count == 2


# ═══════════════════════════════════════════════════════════════════
# 섹션 C. LipsyncPromptBuilder
# ═══════════════════════════════════════════════════════════════════


def test_prompt_builder_says_dog_speaks_lipsync():
    """립싱크 프롬프트는 마스코트가 입을 움직여 말한다고 명시한다(kling과 반대)."""
    prompt = LipsyncPromptBuilder().build_beat("간식 소개")
    assert "lip-sync" in prompt or "speaking" in prompt or "talking" in prompt
    # kling의 'NOT talking'·'mouth stays closed'와 정반대여야 한다
    assert "NOT talking" not in prompt
    assert "stays closed" not in prompt


def test_prompt_builder_forbids_onscreen_text():
    """립싱크여도 화면 자막은 여전히 금지한다(깨진 한글 자막 방지)."""
    prompt = LipsyncPromptBuilder().build_beat("테스트")
    assert "no" in prompt.lower() and "text" in prompt.lower()
    assert "9:16" in prompt


def test_prompt_builder_sanitizes_single_quotes():
    """비트 텍스트의 작은따옴표는 정제된다(인용 구분자 탈출 방지)."""
    prompt = LipsyncPromptBuilder().build_beat("don't worry")
    assert "don't worry" not in prompt  # ASCII 작은따옴표가 치환됨


# ═══════════════════════════════════════════════════════════════════
# 섹션 D. LipsyncBackend.produce_beat_clips
# ═══════════════════════════════════════════════════════════════════


class FakeLipsyncClient:
    """HedraLipsyncClient 대역 — generate 호출 인자를 기록하고 결정적 경로를 반환한다."""

    def __init__(self, video_path: str = "data/fake/lipsync.mp4"):
        self.video_path = video_path
        self.calls: list[tuple[str, str, str]] = []
        self.close_count = 0

    def generate(self, frame_path: str, voice_path: str, prompt: str) -> str:
        self.calls.append((frame_path, voice_path, prompt))
        return self.video_path

    def close(self):
        self.close_count += 1


class FakeTtsClient:
    """ElevenLabsTtsClient 대역 — synthesize 호출을 기록하고 (경로, 초)를 반환한다."""

    def __init__(self, audio_path: str = "data/fake/voice.wav", duration: float = 4.0):
        self.audio_path = audio_path
        self.duration = duration
        self.calls: list[str] = []
        self.close_count = 0

    def synthesize(self, text: str) -> tuple[str, float]:
        self.calls.append(text)
        return self.audio_path, self.duration

    def close(self):
        self.close_count += 1


def test_produce_beat_clips_calls_tts_and_lipsync_per_beat(tmp_path):
    """비트 N개마다 tts.synthesize → lipsync.generate 순서로 호출된다(mux 없음)."""
    lipsync = FakeLipsyncClient()
    tts = FakeTtsClient(duration=4.0)
    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = LipsyncBackend(settings, lipsync_client=lipsync, tts_client=tts)
    clips, total_sec = backend.produce_beat_clips("data/frame.jpg", ["비트1", "비트2", "비트3"])

    assert len(clips) == 3
    # 비트 3개 × audio_sec=4.0 = 12.0초(Hedra 출력 길이 = 입력 음성 길이)
    assert total_sec == pytest.approx(12.0)
    assert tts.calls == ["비트1", "비트2", "비트3"]
    assert len(lipsync.calls) == 3
    # 모든 비트가 같은 frame_path를 쓴다
    assert all(c[0] == "data/frame.jpg" for c in lipsync.calls)
    # lipsync.generate에 tts가 반환한 음성 경로가 전달된다
    assert all(c[1] == "data/fake/voice.wav" for c in lipsync.calls)


def test_produce_beat_clips_returns_order(tmp_path):
    """반환된 클립 경로가 비트 순서와 일치한다."""
    n = [0]

    class _SeqLipsync(FakeLipsyncClient):
        def generate(self, frame_path, voice_path, prompt):
            n[0] += 1
            return f"data/fake/lipsync_{n[0]}.mp4"

    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = LipsyncBackend(settings, lipsync_client=_SeqLipsync(), tts_client=FakeTtsClient())
    clips, _total = backend.produce_beat_clips("frame.jpg", ["A", "B", "C"])
    assert clips == [
        "data/fake/lipsync_1.mp4",
        "data/fake/lipsync_2.mp4",
        "data/fake/lipsync_3.mp4",
    ]


def test_produce_beat_clips_cleans_voice_intermediate_on_success(tmp_path):
    """성공 흐름: 각 비트의 내레이션 WAV 중간물은 영상 생성 후 삭제되고 클립만 남는다."""
    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    counter = {"n": 0}

    class _FileTts:
        def synthesize(self, beat):
            counter["n"] += 1
            p = tmp_path / f"voice_{counter['n']}.wav"
            p.write_bytes(b"RIFFvoice")
            return str(p), 4.0

    class _FileLipsync:
        def generate(self, frame, voice, prompt):
            p = tmp_path / f"lipsync_{counter['n']}.mp4"
            p.write_bytes(b"\x00clip")
            return str(p)

    backend = LipsyncBackend(settings, lipsync_client=_FileLipsync(), tts_client=_FileTts())
    clips, total = backend.produce_beat_clips("frame.png", ["b1", "b2"])

    assert len(clips) == 2
    assert all(Path(c).exists() for c in clips)  # 결과 클립 보존
    assert list(tmp_path.glob("voice_*.wav")) == []  # 내레이션 중간물 정리
    assert total == pytest.approx(8.0)


def test_produce_beat_clips_cleans_completed_on_midloop_failure(tmp_path):
    """중도 실패: 2번 비트 영상 생성 실패 시 1번 완성 클립·모든 내레이션 WAV가 leak 없이 정리된다."""
    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    counter = {"n": 0}

    class _FileTts:
        def synthesize(self, beat):
            counter["n"] += 1
            p = tmp_path / f"voice_{counter['n']}.wav"
            p.write_bytes(b"RIFFvoice")
            return str(p), 4.0

    gen_calls = {"n": 0}

    class _FileLipsync:
        def generate(self, frame, voice, prompt):
            gen_calls["n"] += 1
            if gen_calls["n"] == 2:
                raise VideoRenderError("2번 비트 강제 실패")
            p = tmp_path / f"clip_{gen_calls['n']}.mp4"
            p.write_bytes(b"\x00clip")
            return str(p)

    backend = LipsyncBackend(settings, lipsync_client=_FileLipsync(), tts_client=_FileTts())
    with pytest.raises(VideoRenderError):
        backend.produce_beat_clips("frame.png", ["b1", "b2", "b3"])

    # 1번 완성 클립도 정리되고, 모든 내레이션 WAV도 leak 없음
    assert list(tmp_path.glob("clip_*.mp4")) == []
    assert list(tmp_path.glob("voice_*.wav")) == []


def test_produce_beat_clips_owned_clients_closed_on_success(monkeypatch, tmp_path):
    """클라이언트 미주입 시 자체 생성 후 finally에서 close가 정확히 1회 호출된다."""
    created: dict = {}

    class _OwnedLipsync(FakeLipsyncClient):
        def __init__(self, settings, **kwargs):
            super().__init__()
            created["lipsync"] = self

    class _OwnedTts(FakeTtsClient):
        def __init__(self, settings, **kwargs):
            super().__init__()
            created["tts"] = self

    monkeypatch.setattr(lipsync_module, "HedraLipsyncClient", _OwnedLipsync)
    monkeypatch.setattr(lipsync_module, "ElevenLabsTtsClient", _OwnedTts)

    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = LipsyncBackend(settings)
    backend.produce_beat_clips("frame.jpg", ["비트1"])

    assert created["lipsync"].close_count == 1
    assert created["tts"].close_count == 1


def test_produce_beat_clips_owned_clients_closed_on_failure(monkeypatch, tmp_path):
    """중간에 예외가 발생해도 자체 생성 클라이언트는 finally에서 close된다."""
    created: dict = {}

    class _OwnedLipsync(FakeLipsyncClient):
        def __init__(self, settings, **kwargs):
            super().__init__()
            created["lipsync"] = self

        def generate(self, frame_path, voice_path, prompt):
            raise VideoRenderError("Hedra 실패")

    class _OwnedTts(FakeTtsClient):
        def __init__(self, settings, **kwargs):
            super().__init__()
            created["tts"] = self

    monkeypatch.setattr(lipsync_module, "HedraLipsyncClient", _OwnedLipsync)
    monkeypatch.setattr(lipsync_module, "ElevenLabsTtsClient", _OwnedTts)

    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = LipsyncBackend(settings)
    with pytest.raises(VideoRenderError):
        backend.produce_beat_clips("frame.jpg", ["비트1"])

    assert created["lipsync"].close_count == 1
    assert created["tts"].close_count == 1


def test_produce_beat_clips_injected_clients_not_closed(tmp_path):
    """주입된 클라이언트는 LipsyncBackend가 close하지 않는다(소유권 없음)."""
    lipsync = FakeLipsyncClient()
    tts = FakeTtsClient()
    settings = _lipsync_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = LipsyncBackend(settings, lipsync_client=lipsync, tts_client=tts)
    backend.produce_beat_clips("frame.jpg", ["비트1"])
    assert lipsync.close_count == 0
    assert tts.close_count == 0
