"""Supertone TTS 클라이언트 단위 테스트.

대상: SupertoneTtsClient, _validate_supertone_voice_id, _validate_supertone_model,
     _parse_voice_ids, _wav_duration_sec 등 모듈 헬퍼.

모든 테스트는 fake HTTP 클라이언트(http= 주입)와 fake sleep으로 **네트워크 없이** 동작한다.

섹션 구성:
  A. 정상 합성 흐름(WAV 저장·길이 계산·URL/본문 구성)
  B. 보이스 로테이션(CRC32 결정성·클라이언트 내 고정)
  C. HTTP 오류 분류(4xx·402 크레딧·일시 오류 재시도)
  D. voice_id / model 형식 검증
  E. 키 격리(x-sup-api-key는 supertoneapi.com에만)
  F. Redaction(메시지에 URL·본문 없음)
  G. close / 컨텍스트 매니저
"""

from __future__ import annotations

import io
import wave
import zlib
from pathlib import Path

import pytest

from nutti.config import Settings
from nutti.integrations.tts_supertone import (
    SupertoneTtsClient,
    _MAX_TRANSIENT_RETRIES,
    _parse_voice_ids,
    _validate_supertone_model,
    _validate_supertone_voice_id,
    _wav_duration_sec,
)
from nutti.integrations.video import VideoRenderError

# 기본 보이스 2종(설정 기본값과 동일 — Cheeky(건방진)·Aiko).
_VOICE_A = "d40bae491c78a65f2f8488"
_VOICE_B = "ac449f240c2732b7f0b8bb"


# ─────────────────────────── 공통 헬퍼 ───────────────────────────


def _settings(**overrides) -> Settings:
    """Supertone TTS 클라이언트용 실 경로(non-dry_run) 설정."""
    base: dict = {
        "NUTTI_DRY_RUN": False,
        "SUPERTONE_API_KEY": "test-st-key",
        "NUTTI_SUPERTONE_VOICE_IDS": f"{_VOICE_A},{_VOICE_B}",
        "NUTTI_SUPERTONE_MODEL": "sona_speech_2",
    }
    base.update(overrides)
    return Settings(**base)


def _no_sleep(_seconds: float) -> None:
    """재시도 대기를 즉시 건너뛰는 가짜 sleep."""
    return None


def _make_wav(num_frames: int = 2400, rate: int = 24000) -> bytes:
    """테스트용 유효한 WAV 바이트(16-bit mono, 0으로 채움)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * num_frames)
    return buf.getvalue()


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


class FakeStHttp:
    """SupertoneTtsClient 주입용 fake HTTP 클라이언트.

    post 호출 인자와 헤더를 기록해 x-sup-api-key 격리·URL 구성·body 내용을 검증한다.
    post_responses 큐에서 순서대로 응답을 돌려준다(일시 오류 재시도 검증용).
    """

    def __init__(
        self,
        *,
        post_responses: list[_Resp | Exception] | None = None,
        default_response: _Resp | None = None,
    ):
        self._queue: list[_Resp | Exception] = list(post_responses or [])
        self._default = default_response or _Resp(content=_make_wav())
        self.post_calls: list[tuple[str, dict]] = []  # (url, json)
        self.post_headers: list[dict] = []
        self.closed = False

    def post(self, url, *, headers=None, json=None):
        self.post_calls.append((url, json or {}))
        self.post_headers.append(dict(headers or {}))
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self._default

    def close(self):
        self.closed = True


def _client(tmp_path, fake: FakeStHttp, **overrides) -> SupertoneTtsClient:
    """테스트용 SupertoneTtsClient 인스턴스를 만든다."""
    s = _settings(NUTTI_MEDIA_DIR=str(tmp_path), **overrides)
    return SupertoneTtsClient(s, http=fake, sleep=_no_sleep)


def _expected_voice(text: str, ids: list[str]) -> str:
    """프로덕션과 동일한 CRC32 선택 규칙으로 기대 보이스를 계산한다."""
    return ids[zlib.crc32(text.encode("utf-8")) % len(ids)]


# ═══════════════════════════════════════════════════════════════════
# 섹션 A. 정상 합성 흐름
# ═══════════════════════════════════════════════════════════════════


def test_synthesize_success_saves_wav_and_returns_path(tmp_path):
    """정상 합성: WAV 파일이 media_dir에 저장되고 (경로, 길이초)가 반환된다."""
    fake = FakeStHttp(default_response=_Resp(content=_make_wav()))
    c = _client(tmp_path, fake)

    path, duration = c.synthesize("안녕! 누띠야.")

    assert Path(path).parent == tmp_path
    assert Path(path).name.startswith("st_voice_")
    assert Path(path).suffix == ".wav"
    assert Path(path).read_bytes() == _make_wav()
    assert duration > 0


def test_synthesize_duration_from_header(tmp_path):
    """X-Audio-Length 헤더가 있으면 그 값을 길이로 쓴다(WAV 파싱보다 우선)."""
    fake = FakeStHttp(
        default_response=_Resp(content=_make_wav(), headers={"X-Audio-Length": "6.426122"})
    )
    c = _client(tmp_path, fake)
    _, duration = c.synthesize("테스트")
    assert duration == pytest.approx(6.426122)


def test_synthesize_duration_fallback_parses_wav(tmp_path):
    """헤더가 없으면 WAV 헤더 파싱으로 길이를 계산한다(frames / rate)."""
    num_frames, rate = 12000, 24000  # 0.5초
    fake = FakeStHttp(default_response=_Resp(content=_make_wav(num_frames, rate)))
    c = _client(tmp_path, fake)
    _, duration = c.synthesize("테스트")
    assert duration == pytest.approx(num_frames / rate)


def test_synthesize_invalid_header_falls_back_to_wav(tmp_path):
    """X-Audio-Length가 숫자가 아니면 무시하고 WAV 파싱으로 폴백한다."""
    num_frames, rate = 24000, 24000  # 1.0초
    fake = FakeStHttp(
        default_response=_Resp(
            content=_make_wav(num_frames, rate), headers={"X-Audio-Length": "abc"}
        )
    )
    c = _client(tmp_path, fake)
    _, duration = c.synthesize("테스트")
    assert duration == pytest.approx(1.0)


def test_synthesize_uses_correct_url_and_body(tmp_path):
    """URL에 선택된 voice_id가, 본문에 language='ko'·model·output_format='wav'가 들어간다."""
    fake = FakeStHttp()
    c = _client(tmp_path, fake)
    c.synthesize("반갑습니다")

    url, body = fake.post_calls[0]
    assert "supertoneapi.com" in url
    expected = _expected_voice("반갑습니다", [_VOICE_A, _VOICE_B])
    assert url.endswith(f"/{expected}")
    assert body.get("language") == "ko"
    assert body.get("model") == "sona_speech_2"
    assert body.get("output_format") == "wav"
    assert body.get("text") == "반갑습니다"


def test_synthesize_empty_text_raises(tmp_path):
    """빈 텍스트(공백만) 입력은 VideoRenderError를 던진다."""
    fake = FakeStHttp()
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="비어"):
        c.synthesize("   ")


# ═══════════════════════════════════════════════════════════════════
# 섹션 B. 보이스 로테이션
# ═══════════════════════════════════════════════════════════════════


def test_voice_pick_is_deterministic_by_text(tmp_path):
    """같은 첫 대사 → 항상 같은 보이스(CRC32, salt 없음 — 재현 가능)."""
    for _ in range(2):
        fake = FakeStHttp()
        c = _client(tmp_path, fake)
        c.synthesize("결정적 선택 테스트")
        url, _ = fake.post_calls[0]
        assert url.endswith(f"/{_expected_voice('결정적 선택 테스트', [_VOICE_A, _VOICE_B])}")


def test_voice_sticky_within_client(tmp_path):
    """한 클라이언트(=영상 1편)의 모든 비트는 첫 선택 보이스를 유지한다.

    비트마다 보이스가 바뀌면 한 영상 안에서 목소리가 오락가락한다 — 핵심 계약 핀.
    """
    fake = FakeStHttp()
    c = _client(tmp_path, fake)
    c.synthesize("첫 번째 비트")
    c.synthesize("두 번째 비트")
    c.synthesize("세 번째 비트")
    first_url = fake.post_calls[0][0]
    assert all(url == first_url for url, _ in fake.post_calls)


def test_voice_single_id_always_used(tmp_path):
    """voice_ids가 1개면 항상 그 보이스를 쓴다."""
    fake = FakeStHttp()
    c = _client(tmp_path, fake, NUTTI_SUPERTONE_VOICE_IDS=_VOICE_A)
    c.synthesize("아무 대사")
    url, _ = fake.post_calls[0]
    assert url.endswith(f"/{_VOICE_A}")


# ═══════════════════════════════════════════════════════════════════
# 섹션 C. HTTP 오류 분류 및 재시도
# ═══════════════════════════════════════════════════════════════════


def test_synthesize_http_4xx_raises_render_error(tmp_path):
    """HTTP 4xx(예: 422)는 VideoRenderError로 전파된다."""
    fake = FakeStHttp(default_response=_Resp(status_code=422))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="422"):
        c.synthesize("테스트")


def test_synthesize_http_402_mentions_credit(tmp_path):
    """HTTP 402(크레딧 소진)는 충전 안내가 포함된 메시지로 즉시 실패한다(재시도 없음).

    Gemini 크레딧 소진 오진 사례(2026-06-10)의 교훈 — 결제성 오류는 명시적으로 구분한다.
    """
    fake = FakeStHttp(default_response=_Resp(status_code=402))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="크레딧"):
        c.synthesize("테스트")
    assert len(fake.post_calls) == 1


def test_synthesize_transient_429_retries_and_succeeds(tmp_path):
    """일시 오류 429 1회 후 재시도해 성공한다."""
    sleeps: list[float] = []
    fake = FakeStHttp(post_responses=[_Resp(status_code=429), _Resp(content=_make_wav())])
    s = _settings(NUTTI_MEDIA_DIR=str(tmp_path))
    c = SupertoneTtsClient(s, http=fake, sleep=sleeps.append)
    path, _ = c.synthesize("안녕하세요")

    assert Path(path).exists()
    assert len(fake.post_calls) == 2
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_synthesize_transient_5xx_exhausted_raises(tmp_path):
    """연속 503이 재시도 한도를 넘으면 VideoRenderError."""
    fake = FakeStHttp(
        post_responses=[_Resp(status_code=503)] * (_MAX_TRANSIENT_RETRIES + 2)
    )
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="503"):
        c.synthesize("테스트")
    assert len(fake.post_calls) == 1 + _MAX_TRANSIENT_RETRIES


def test_synthesize_transport_error_raises_render_error(tmp_path):
    """전송 계층 오류(ConnectionError)는 VideoRenderError로 승격된다."""
    fake = FakeStHttp(post_responses=[ConnectionError("네트워크 차단")])
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        c.synthesize("테스트")


def test_synthesize_empty_audio_content_raises(tmp_path):
    """200 응답이지만 content가 빈 바이트이면 VideoRenderError."""
    fake = FakeStHttp(default_response=_Resp(status_code=200, content=b""))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="오디오 바이트"):
        c.synthesize("테스트")


def test_wav_duration_sec_rejects_garbage():
    """WAV가 아닌 바이트는 파싱 단계에서 VideoRenderError(타입명만 노출)."""
    with pytest.raises(VideoRenderError, match="WAV 파싱 실패"):
        _wav_duration_sec(b"not-a-wav")


# ═══════════════════════════════════════════════════════════════════
# 섹션 D. voice_id / model 형식 검증
# ═══════════════════════════════════════════════════════════════════


def test_validate_voice_id_allows_valid():
    assert _validate_supertone_voice_id(_VOICE_A) == _VOICE_A


@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "voice/inject", "voice?q=1", "voice:8080", "voice id", "a" * 129, "v.id"],
)
def test_validate_voice_id_rejects_malformed(bad_id):
    with pytest.raises(VideoRenderError, match="NUTTI_SUPERTONE_VOICE_IDS"):
        _validate_supertone_voice_id(bad_id)


def test_parse_voice_ids_splits_and_strips():
    """콤마 구분 목록이 공백 제거·빈 항목 제거 후 순서대로 반환된다."""
    assert _parse_voice_ids(f" {_VOICE_A} , {_VOICE_B} ,") == [_VOICE_A, _VOICE_B]


def test_parse_voice_ids_empty_raises():
    with pytest.raises(VideoRenderError, match="비어"):
        _parse_voice_ids("  , ,")


def test_validate_model_allows_valid():
    assert _validate_supertone_model("sona_speech_2") == "sona_speech_2"


@pytest.mark.parametrize(
    "bad_mid", ["", "Sona_Speech", "model id", "model?x", "model/path", "a" * 65]
)
def test_validate_model_rejects_malformed(bad_mid):
    with pytest.raises(VideoRenderError, match="NUTTI_SUPERTONE_MODEL"):
        _validate_supertone_model(bad_mid)


def test_client_init_rejects_malformed_voice_id(tmp_path):
    """생성 시점에 voice_ids 중 하나라도 잘못되면 VideoRenderError."""
    s = _settings(NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_SUPERTONE_VOICE_IDS="ok123,bad/id")
    with pytest.raises(VideoRenderError, match="NUTTI_SUPERTONE_VOICE_IDS"):
        SupertoneTtsClient(s, http=FakeStHttp(), sleep=_no_sleep)


# ═══════════════════════════════════════════════════════════════════
# 섹션 E. 키 격리 — x-sup-api-key는 supertoneapi.com에만
# ═══════════════════════════════════════════════════════════════════


def test_synthesize_api_key_header_matches_settings(tmp_path):
    """x-sup-api-key 헤더 값이 settings.supertone_api_key와 일치한다."""
    fake = FakeStHttp()
    c = _client(tmp_path, fake, SUPERTONE_API_KEY="my-secret-st-key")
    c.synthesize("테스트")

    key_map = {k.lower(): v for k, v in fake.post_headers[0].items()}
    assert key_map.get("x-sup-api-key") == "my-secret-st-key"


def test_synthesize_request_url_is_supertone_host(tmp_path):
    """요청 URL 호스트가 supertoneapi.com이다(타 호스트에 키를 보내지 않는다)."""
    fake = FakeStHttp()
    c = _client(tmp_path, fake)
    c.synthesize("테스트")
    url, _ = fake.post_calls[0]
    assert url.startswith("https://supertoneapi.com/")


# ═══════════════════════════════════════════════════════════════════
# 섹션 F. Redaction — 오류 메시지에 URL·요청 본문 없음
# ═══════════════════════════════════════════════════════════════════


def test_http_error_message_does_not_contain_url(tmp_path):
    """HTTP 오류 메시지에 요청 URL이 포함되지 않는다(redaction)."""
    fake = FakeStHttp(default_response=_Resp(status_code=500))
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError) as exc_info:
        c.synthesize("테스트")
    err_msg = str(exc_info.value)
    assert "supertoneapi.com" not in err_msg
    assert "text-to-speech" not in err_msg


def test_transport_error_message_does_not_contain_body(tmp_path):
    """전송 오류 메시지에 요청 본문(대사)이 포함되지 않는다(redaction)."""
    fake = FakeStHttp(post_responses=[ConnectionError("secret-body-leak")])
    c = _client(tmp_path, fake)
    with pytest.raises(VideoRenderError) as exc_info:
        c.synthesize("민감한 대사 내용입니다")
    err_msg = str(exc_info.value)
    assert "민감한 대사" not in err_msg
    assert "supertoneapi.com" not in err_msg


# ═══════════════════════════════════════════════════════════════════
# 섹션 G. close / 컨텍스트 매니저
# ═══════════════════════════════════════════════════════════════════


def test_close_closes_injected_http(tmp_path):
    fake = FakeStHttp()
    c = _client(tmp_path, fake)
    c.close()
    assert fake.closed is True


def test_context_manager_closes_on_exit(tmp_path):
    fake = FakeStHttp()
    s = _settings(NUTTI_MEDIA_DIR=str(tmp_path))
    with SupertoneTtsClient(s, http=fake, sleep=_no_sleep) as c:
        c.synthesize("테스트")
    assert fake.closed is True


def test_close_is_idempotent(tmp_path):
    fake = FakeStHttp()
    c = _client(tmp_path, fake)
    c.close()
    c.close()
    assert fake.closed is True
