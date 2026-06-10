"""ElevenLabs TTS 클라이언트 단위 테스트.

대상: ElevenLabsTtsClient, _validate_voice_id, _validate_elevenlabs_model_id,
     _elevenlabs_headers 등 모듈 헬퍼.

모든 테스트는 fake HTTP 클라이언트(http= 주입)와 fake sleep으로
**네트워크 없이** 동작한다.

섹션 구성:
  A. 정상 합성 흐름(WAV 저장·길이 계산·URL 구성)
  B. HTTP 오류 분류(4xx·5xx·일시 오류 재시도)
  C. 응답 파싱 오류(비-JSON·오디오 누락)
  D. voice_id / model_id 형식 검증
  E. 키 격리(CDN에 xi-api-key 미첨부)
  F. Redaction(메시지에 URL·본문 없음)
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from nutti.config import Settings
from nutti.integrations.tts_elevenlabs import (
    ElevenLabsTtsClient,
    _MAX_TRANSIENT_RETRIES,
    _PCM_BYTES_PER_SAMPLE,
    _PCM_SAMPLE_RATE,
    _validate_elevenlabs_model_id,
    _validate_voice_id,
)
from nutti.integrations.video import VideoRenderError


# ─────────────────────────── 공통 헬퍼 ───────────────────────────


def _settings(**overrides) -> Settings:
    """ElevenLabs TTS 클라이언트용 실 경로(non-dry_run) 설정."""
    base: dict = {
        "NUTTI_DRY_RUN": False,
        "ELEVENLABS_API_KEY": "test-el-key",
        "NUTTI_ELEVENLABS_VOICE_ID": "21m00Tcm4TlvDq8ikWAM",  # 기본 Rachel
        "NUTTI_ELEVENLABS_MODEL": "eleven_multilingual_v2",
    }
    base.update(overrides)
    return Settings(**base)


def _no_sleep(_seconds: float) -> None:
    """폴링/재시도 대기를 즉시 건너뛰는 가짜 sleep."""
    return None


def _make_pcm(num_samples: int = 240) -> bytes:
    """테스트용 16-bit mono PCM 더미 바이트(0으로 채움)."""
    return b"\x00\x00" * num_samples


# ─────────────────────────── Fake HTTP ───────────────────────────


class _Resp:
    """httpx.Response 대역(status_code + content + headers)."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self.content = content
        self.headers = dict(headers or {})


class FakeElHttp:
    """ElevenLabsTtsClient 주입용 fake HTTP 클라이언트.

    post 호출 인자와 헤더를 기록해 xi-api-key 격리·URL 구성·body 내용을 검증한다.
    post_responses 리스트에서 순서대로 응답을 돌려준다(일시 오류 재시도 검증용).
    """

    def __init__(
        self,
        *,
        post_responses: list[_Resp | Exception] | None = None,
        default_response: _Resp | None = None,
    ):
        # 순서대로 꺼낼 응답 큐. 비면 default_response를 계속 반환한다.
        self._queue: list[_Resp | Exception] = list(post_responses or [])
        self._default = default_response or _Resp(content=_make_pcm())
        self.post_calls: list[tuple[str, dict, dict]] = []  # (url, params, json)
        self.post_headers: list[dict] = []
        self.closed = False

    def post(self, url, *, headers=None, params=None, json=None):
        self.post_calls.append((url, params or {}, json or {}))
        self.post_headers.append(dict(headers or {}))
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self._default

    def close(self):
        self.closed = True


def _client(tmp_path, fake: FakeElHttp, **overrides) -> ElevenLabsTtsClient:
    """테스트용 ElevenLabsTtsClient 인스턴스를 만든다."""
    s = _settings(NUTTI_MEDIA_DIR=str(tmp_path), **overrides)
    return ElevenLabsTtsClient(s, http=fake, sleep=_no_sleep)


# ═══════════════════════════════════════════════════════════════════
# 섹션 A. 정상 합성 흐름
# ═══════════════════════════════════════════════════════════════════


def test_synthesize_success_saves_wav_and_returns_path(tmp_path):
    """정상 합성: WAV 파일이 media_dir에 저장되고 (경로, 길이초)가 반환된다."""
    num_samples = 2400
    pcm = _make_pcm(num_samples)
    fake = FakeElHttp(default_response=_Resp(content=pcm))
    c = _client(tmp_path, fake)

    path, duration = c.synthesize("안녕하세요, 저는 누티예요!")

    assert Path(path).parent == tmp_path
    assert Path(path).name.startswith("el_voice_")
    assert Path(path).suffix == ".wav"
    # 파일이 실제로 존재하고 유효한 WAV 구조를 가진다.
    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == _PCM_SAMPLE_RATE


def test_synthesize_duration_calculation(tmp_path):
    """길이(초) = len(pcm) / (rate * 2) 공식이 정확히 적용된다."""
    num_samples = 4800  # 0.1초 @ 24kHz
    pcm = _make_pcm(num_samples)
    expected = (len(pcm) / _PCM_BYTES_PER_SAMPLE) / _PCM_SAMPLE_RATE

    fake = FakeElHttp(default_response=_Resp(content=pcm))
    c = _client(tmp_path, fake)
    _, duration = c.synthesize("테스트")

    assert abs(duration - expected) < 1e-9


def test_synthesize_uses_correct_url(tmp_path):
    """요청 URL에 voice_id가 올바르게 포함된다."""
    voice_id = "21m00Tcm4TlvDq8ikWAM"
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    c = _client(tmp_path, fake, NUTTI_ELEVENLABS_VOICE_ID=voice_id)
    c.synthesize("테스트")

    url, _, _ = fake.post_calls[0]
    assert url.endswith(f"/{voice_id}")
    assert "api.elevenlabs.io" in url


def test_synthesize_uses_pcm_output_format(tmp_path):
    """output_format=pcm_24000 쿼리 파라미터가 요청에 포함된다."""
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    c = _client(tmp_path, fake)
    c.synthesize("테스트")

    _, params, _ = fake.post_calls[0]
    assert params.get("output_format") == "pcm_24000"


def test_synthesize_request_body_has_model_and_language(tmp_path):
    """요청 본문에 model_id, language_code='ko', voice_settings가 포함된다."""
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    c = _client(tmp_path, fake, NUTTI_ELEVENLABS_MODEL="eleven_flash_v2_5")
    c.synthesize("반갑습니다")

    _, _, body = fake.post_calls[0]
    assert body.get("model_id") == "eleven_flash_v2_5"
    assert body.get("language_code") == "ko"
    assert "voice_settings" in body
    assert body.get("text") == "반갑습니다"


def test_synthesize_empty_text_raises(tmp_path):
    """빈 텍스트(공백만) 입력은 VideoRenderError를 던진다."""
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="비어"):
        c.synthesize("   ")


# ═══════════════════════════════════════════════════════════════════
# 섹션 B. HTTP 오류 분류 및 일시 오류 재시도
# ═══════════════════════════════════════════════════════════════════


def test_synthesize_http_4xx_raises_render_error(tmp_path):
    """HTTP 4xx(예: 422 유효성 검증 오류)는 VideoRenderError로 전파된다."""
    fake = FakeElHttp(default_response=_Resp(status_code=422, content=b""))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="422"):
        c.synthesize("테스트")


def test_synthesize_http_401_raises_render_error(tmp_path):
    """HTTP 401(인증 실패)는 VideoRenderError로 전파된다."""
    fake = FakeElHttp(default_response=_Resp(status_code=401, content=b""))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="401"):
        c.synthesize("테스트")


def test_synthesize_http_400_does_not_retry(tmp_path):
    """HTTP 400은 영구 오류 — 재시도 없이 즉시 VideoRenderError를 던진다."""
    fake = FakeElHttp(default_response=_Resp(status_code=400, content=b""))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        c.synthesize("테스트")
    # 재시도 없이 1회 요청만 해야 한다.
    assert len(fake.post_calls) == 1


def test_synthesize_transient_429_retries_and_succeeds(tmp_path):
    """일시 오류 429 1회 후 재시도해 성공한다."""
    sleeps: list[float] = []
    pcm = _make_pcm()
    fake = FakeElHttp(
        post_responses=[
            _Resp(status_code=429, content=b""),
            _Resp(content=pcm),
        ]
    )
    s = _settings(NUTTI_MEDIA_DIR=str(tmp_path))
    c = ElevenLabsTtsClient(s, http=fake, sleep=sleeps.append)
    path, _ = c.synthesize("안녕하세요")

    assert Path(path).exists()
    assert len(fake.post_calls) == 2   # 429 1회 + 재시도 1회
    assert len(sleeps) == 1            # backoff sleep 1회
    assert sleeps[0] > 0


def test_synthesize_transient_5xx_retries_and_succeeds(tmp_path):
    """일시 오류 503 1회 후 재시도해 성공한다."""
    pcm = _make_pcm()
    fake = FakeElHttp(
        post_responses=[
            _Resp(status_code=503, content=b""),
            _Resp(content=pcm),
        ]
    )
    c = _client(tmp_path, fake)
    path, _ = c.synthesize("테스트")

    assert Path(path).exists()
    assert len(fake.post_calls) == 2


def test_synthesize_transient_429_exhausted_raises(tmp_path):
    """연속 429가 재시도 한도(_MAX_TRANSIENT_RETRIES)를 넘으면 VideoRenderError."""
    fake = FakeElHttp(
        post_responses=[_Resp(status_code=429, content=b"")] * (_MAX_TRANSIENT_RETRIES + 2)
    )
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="429"):
        c.synthesize("테스트")
    # 최초 1회 + 재시도 3회 = 4회
    assert len(fake.post_calls) == 1 + _MAX_TRANSIENT_RETRIES


def test_synthesize_transport_error_raises_render_error(tmp_path):
    """전송 계층 오류(ConnectionError)는 VideoRenderError로 승격된다."""
    fake = FakeElHttp(post_responses=[ConnectionError("네트워크 차단")])
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        c.synthesize("테스트")


# ═══════════════════════════════════════════════════════════════════
# 섹션 C. 응답 파싱 오류
# ═══════════════════════════════════════════════════════════════════


def test_synthesize_empty_audio_content_raises(tmp_path):
    """200 응답이지만 content가 빈 바이트이면 VideoRenderError."""
    fake = FakeElHttp(default_response=_Resp(status_code=200, content=b""))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="오디오 바이트"):
        c.synthesize("테스트")


def test_synthesize_no_status_code_raises(tmp_path):
    """status_code 속성이 없는 응답 객체는 VideoRenderError."""

    class _NoCodeResp:
        content = b"some bytes"
        headers = {}

    class _BadHttp:
        def post(self, *a, **kw):
            return _NoCodeResp()

        def close(self):
            pass

    s = _settings(NUTTI_MEDIA_DIR=str(tmp_path))
    c = ElevenLabsTtsClient(s, http=_BadHttp(), sleep=_no_sleep)
    with pytest.raises(VideoRenderError, match="status_code"):
        c.synthesize("테스트")


# ═══════════════════════════════════════════════════════════════════
# 섹션 D. voice_id / model_id 형식 검증
# ═══════════════════════════════════════════════════════════════════


def test_validate_voice_id_allows_valid():
    """영숫자만 있는 정상 voice_id는 그대로 반환된다."""
    result = _validate_voice_id("21m00Tcm4TlvDq8ikWAM")
    assert result == "21m00Tcm4TlvDq8ikWAM"


def test_validate_voice_id_allows_uuid_cloned_voice():
    """UUIDv4 꼴(하이픈 포함) 사용자 생성/클론 voice_id는 허용한다.

    ElevenLabs 클론/생성 보이스는 프리메이드(영숫자 20자)와 달리
    550e8400-e29b-41d4-a716-446655440000 같은 UUID 식별자를 쓴다 — 하이픈은
    URL 경로를 변조하지 않으므로 통과해야 한다(과거엔 잘못 거부됐음).
    """
    uuid_id = "550e8400-e29b-41d4-a716-446655440000"
    assert _validate_voice_id(uuid_id) == uuid_id


@pytest.mark.parametrize(
    "bad_id",
    [
        "",                          # 빈 문자열
        "   ",                       # 공백만
        "voice/inject",              # 슬래시 — URL 경로 변조
        "voice?q=1",                 # 쿼리 주입
        "voice:8080",                # 포트/스킴 변조
        "voice id",                  # 공백
        "a" * 129,                   # 길이 초과
        "voice.id",                  # 점 — URL 변조 가능 문자, 여전히 불허
        "voice_id",                  # 언더스코어 불허(voice_id는 영숫자·하이픈만)
    ],
)
def test_validate_voice_id_rejects_malformed(bad_id):
    """허용 외 형태는 VideoRenderError를 던진다."""
    with pytest.raises(VideoRenderError, match="NUTTI_ELEVENLABS_VOICE_ID"):
        _validate_voice_id(bad_id)


def test_validate_elevenlabs_model_id_allows_valid():
    """소문자 영숫자·`_`만 있는 model_id는 그대로 반환된다."""
    result = _validate_elevenlabs_model_id("eleven_multilingual_v2")
    assert result == "eleven_multilingual_v2"


@pytest.mark.parametrize(
    "bad_mid",
    [
        "",
        "   ",
        "model id",                  # 공백
        "Eleven_Model",              # 대문자 불허
        "model?q=inject",            # 쿼리 주입
        "model:ver",                 # `:` 불허
        "model/path",                # 슬래시 불허
        "a" * 65,                    # 길이 초과
    ],
)
def test_validate_elevenlabs_model_id_rejects_malformed(bad_mid):
    """허용 외 형태는 VideoRenderError를 던진다."""
    with pytest.raises(VideoRenderError, match="NUTTI_ELEVENLABS_MODEL"):
        _validate_elevenlabs_model_id(bad_mid)


def test_client_init_rejects_malformed_voice_id(tmp_path):
    """생성 시점에 voice_id가 잘못되면 VideoRenderError를 던진다."""
    s = _settings(
        NUTTI_MEDIA_DIR=str(tmp_path),
        NUTTI_ELEVENLABS_VOICE_ID="bad/voice?id",
    )
    with pytest.raises(VideoRenderError, match="NUTTI_ELEVENLABS_VOICE_ID"):
        ElevenLabsTtsClient(s, http=FakeElHttp(), sleep=_no_sleep)


def test_client_init_rejects_malformed_model_id(tmp_path):
    """생성 시점에 model_id가 잘못되면 VideoRenderError를 던진다."""
    s = _settings(
        NUTTI_MEDIA_DIR=str(tmp_path),
        NUTTI_ELEVENLABS_MODEL="BAD MODEL",
    )
    with pytest.raises(VideoRenderError, match="NUTTI_ELEVENLABS_MODEL"):
        ElevenLabsTtsClient(s, http=FakeElHttp(), sleep=_no_sleep)


# ═══════════════════════════════════════════════════════════════════
# 섹션 E. 키 격리 — xi-api-key는 api.elevenlabs.io에만 첨부
# ═══════════════════════════════════════════════════════════════════


def test_synthesize_request_has_xi_api_key_header(tmp_path):
    """TTS 요청 헤더에 xi-api-key가 포함된다."""
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    c = _client(tmp_path, fake)
    c.synthesize("테스트")

    headers = fake.post_headers[0]
    assert "xi-api-key" in {k.lower() for k in headers}


def test_synthesize_api_key_value_matches_settings(tmp_path):
    """xi-api-key 값이 settings.elevenlabs_api_key와 일치한다."""
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    c = _client(tmp_path, fake, ELEVENLABS_API_KEY="my-secret-key-xyz")
    c.synthesize("테스트")

    headers = fake.post_headers[0]
    # 헤더 키는 소문자로 정규화해 검색
    key_map = {k.lower(): v for k, v in headers.items()}
    assert key_map.get("xi-api-key") == "my-secret-key-xyz"


def test_synthesize_request_url_is_elevenlabs_host(tmp_path):
    """요청 URL 호스트가 api.elevenlabs.io이다(타 호스트에 키를 보내지 않는다)."""
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    c = _client(tmp_path, fake)
    c.synthesize("테스트")

    url, _, _ = fake.post_calls[0]
    assert "api.elevenlabs.io" in url


# ═══════════════════════════════════════════════════════════════════
# 섹션 F. Redaction — 오류 메시지에 URL·요청 본문 없음
# ═══════════════════════════════════════════════════════════════════


def test_http_error_message_does_not_contain_url(tmp_path):
    """HTTP 오류 메시지에 요청 URL이 포함되지 않는다(redaction)."""
    fake = FakeElHttp(default_response=_Resp(status_code=500, content=b""))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError) as exc_info:
        c.synthesize("테스트")
    err_msg = str(exc_info.value)
    assert "api.elevenlabs.io" not in err_msg
    assert "text-to-speech" not in err_msg


def test_transport_error_message_does_not_contain_body(tmp_path):
    """전송 오류 메시지에 요청 본문이 포함되지 않는다(redaction)."""
    fake = FakeElHttp(post_responses=[ConnectionError("secret-body-leak")])
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError) as exc_info:
        c.synthesize("민감한 대사 내용입니다")
    err_msg = str(exc_info.value)
    # 요청 본문(대사)·URL이 오류 메시지에 누출되지 않아야 한다.
    assert "민감한 대사" not in err_msg
    assert "api.elevenlabs.io" not in err_msg


# ═══════════════════════════════════════════════════════════════════
# 섹션 G. _HttpClosingMixin — close / 컨텍스트 매니저
# ═══════════════════════════════════════════════════════════════════


def test_close_closes_injected_http(tmp_path):
    """close()가 주입된 fake HTTP 클라이언트를 닫는다."""
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    c = _client(tmp_path, fake)
    c.close()
    assert fake.closed is True


def test_context_manager_closes_on_exit(tmp_path):
    """with 문 종료 시 HTTP 클라이언트가 닫힌다."""
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    s = _settings(NUTTI_MEDIA_DIR=str(tmp_path))
    with ElevenLabsTtsClient(s, http=fake, sleep=_no_sleep) as c:
        c.synthesize("테스트")
    assert fake.closed is True


def test_close_is_idempotent(tmp_path):
    """close()를 여러 번 호출해도 오류 없이 멱등하게 동작한다."""
    fake = FakeElHttp(default_response=_Resp(content=_make_pcm()))
    c = _client(tmp_path, fake)
    c.close()
    c.close()  # 두 번째 호출도 예외 없이 통과해야 한다.
    assert fake.closed is True
