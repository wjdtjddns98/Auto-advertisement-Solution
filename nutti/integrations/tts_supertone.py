"""Supertone TTS 클라이언트 — 한국어 캐릭터 보이스 음성 합성.

GeminiTtsClient/ElevenLabsTtsClient와 동일한 공개 계약을 따른다:
  synthesize(text) -> (audio_path: str, duration_sec: float)

흐름: POST https://supertoneapi.com/v1/text-to-speech/{voice_id}
  output_format=wav 로 WAV 바이트를 받아 그대로 media_dir에 저장하고
  (경로, 길이초)를 반환한다. ElevenLabs와 달리 PCM→WAV 변환이 불필요하다.

길이 계산: 응답 헤더 `X-Audio-Length`(초 단위 float, 2026-06-12 라이브 검증)를
우선 사용하고, 없으면 WAV 헤더를 직접 파싱해 폴백한다.

보이스 로테이션: NUTTI_SUPERTONE_VOICE_IDS(콤마 구분 복수) 중 첫 합성 텍스트의
CRC32로 1개를 결정적으로 선택한다 — 클라이언트는 영상 1편당 1개 생성되므로
한 영상 안에서는 같은 보이스가 유지되고, 대본이 다른 영상에서는 다른 보이스가
나올 수 있다(PO 선정 2종 Cheeky(건방진)·Aiko를 모두 활용).

계약(video.py / video_kling.py와 동일 원칙):
- 모든 오류는 VideoRenderError로만 전파(상태 코드·예외 타입명만 노출,
  URL·응답 본문·요청 ID 미노출 — redaction). 402는 "크레딧 소진" 안내를 덧붙인다.
- x-sup-api-key 헤더는 supertoneapi.com 요청에만 첨부(타 호스트 유출 금지).
- voice_id·model 은 URL/본문 삽입 전 정규식 검증(주입 표면 한정).
- _HttpClosingMixin으로 연결 풀 정리, _write_bytes 원자적 저장.
- 일시 오류(429/5xx)는 지수 backoff로 최대 3회 재시도.
- dry_run 게이트는 상위 백엔드(VideoStudio)가 담당 — 이 클라이언트는
  실 경로에서만 생성된다.
"""

from __future__ import annotations

import io
import re
import time
import wave
import zlib
from pathlib import Path
from uuid import uuid4

from nutti.config import Settings
from nutti.integrations.video import (
    VideoRenderError,
    _HttpClosingMixin,
    _safe_send,
    _sanitize_prompt_text,
    _write_bytes,
)
from nutti.logging import get_logger

log = get_logger(__name__)

# Supertone TTS 엔드포인트. 자격증명(x-sup-api-key)은 이 호스트 요청에만 붙인다.
_SUPERTONE_TTS_BASE = "https://supertoneapi.com/v1/text-to-speech"

# voice_id 허용 형태: 영숫자·`-` 1~128자(Supertone id는 hex 22자 — ElevenLabs와 동일 원칙).
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")
_MAX_VOICE_ID_CHARS = 128

# model 허용 형태: 소문자 영숫자·`_`(예: sona_speech_2, supertonic_api_3).
_MODEL_RE = re.compile(r"^[a-z0-9_]+$")
_MAX_MODEL_CHARS = 64

# 내레이션 TTS 입력 길이 상한. Supertone API의 요청당 하드 리밋이 300자라
# (비트 1개 대사는 45자 이내) 이를 그대로 따른다 — 초과분은 sanitize가 자른다.
_MAX_TTS_CHARS = 300

# 일시 오류(429/5xx) 최대 재시도 횟수와 backoff 기준(초). video_kling과 동일.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_SEC = 2.0


def _validate_supertone_voice_id(voice_id: str) -> str:
    """voice_id를 URL 경로에 삽입하기 전에 형식을 검증한다.

    `:`, `?`, `/`, 공백 등 URL 구조를 변조할 문자가 들어가면 엔드포인트가
    바뀔 수 있으므로 거부한다. 실패는 VideoRenderError(환경변수 이름만 노출).
    """
    vid = (voice_id or "").strip()
    if not vid or len(vid) > _MAX_VOICE_ID_CHARS or not _VOICE_ID_RE.match(vid):
        raise VideoRenderError(
            f"Supertone voice_id(NUTTI_SUPERTONE_VOICE_IDS) 형식이 올바르지 않습니다 "
            f"(길이 {len(voice_id or '')})"
        )
    return vid


def _validate_supertone_model(model: str) -> str:
    """model을 요청 본문에 삽입하기 전에 형식을 검증한다(소문자 영숫자·`_`만)."""
    mid = (model or "").strip()
    if not mid or len(mid) > _MAX_MODEL_CHARS or not _MODEL_RE.match(mid):
        raise VideoRenderError("Supertone model(NUTTI_SUPERTONE_MODEL) 형식이 올바르지 않습니다")
    return mid


def _parse_voice_ids(raw: str) -> list[str]:
    """콤마 구분 voice_id 목록을 파싱·검증한다(빈 항목 제거, 최소 1개 필수)."""
    ids = [v.strip() for v in (raw or "").split(",") if v.strip()]
    if not ids:
        raise VideoRenderError("Supertone voice_id(NUTTI_SUPERTONE_VOICE_IDS)가 비어 있습니다")
    return [_validate_supertone_voice_id(v) for v in ids]


def _supertone_headers(settings: Settings) -> dict:
    """Supertone TTS 인증 헤더(`x-sup-api-key`). supertoneapi.com 요청에만 첨부한다."""
    return {
        "x-sup-api-key": settings.supertone_api_key,
        "Content-Type": "application/json",
    }


def _wav_duration_sec(data: bytes) -> float:
    """WAV 바이트에서 길이(초)를 파싱한다. 실패는 VideoRenderError."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.getnframes()
    except (wave.Error, EOFError, OSError) as exc:
        raise VideoRenderError(
            f"Supertone TTS 응답 WAV 파싱 실패: {type(exc).__name__}"
        ) from exc
    if rate <= 0:
        raise VideoRenderError("Supertone TTS 응답 WAV의 샘플레이트가 유효하지 않습니다")
    return frames / rate


class SupertoneTtsClient(_HttpClosingMixin):
    """Supertone REST TTS로 한국어 캐릭터 보이스 음성을 합성하는 클라이언트.

    `POST /v1/text-to-speech/{voice_id}` 에 텍스트를 보내고 WAV 바이트를 받아
    media_dir에 저장한 뒤 (경로, 길이초)를 반환한다.

    GeminiTtsClient와 동일한 공개 계약:
      synthesize(text: str) -> tuple[str, float]  # (wav_path, duration_sec)

    모든 오류는 VideoRenderError로 전파(상태 코드·예외 타입명만 노출).
    테스트는 http= 인자로 fake HTTP 클라이언트를 주입해 네트워크 없이 검증한다.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep if sleep is not None else time.sleep
        # voice_id 목록·model은 URL/본문에 삽입되므로 생성 시점에 형식 검증한다.
        self._voice_ids = _parse_voice_ids(settings.supertone_voice_ids)
        self._model = _validate_supertone_model(settings.supertone_model)
        # 첫 synthesize에서 대본 기반으로 결정되는 보이스(영상 1편 동안 고정).
        self._voice_id: str | None = None

    def _client(self):
        """httpx.Client를 지연 생성한다(주입된 fake가 있으면 그대로 반환)."""
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    def _pick_voice(self, first_text: str) -> str:
        """첫 합성 텍스트의 CRC32로 보이스를 결정적으로 선택한다(이후 재사용).

        salt 없는 CRC32라 같은 대본이면 항상 같은 보이스가 나온다(재현 가능).
        한 클라이언트(=영상 1편)의 모든 비트는 같은 보이스를 쓴다.
        """
        if self._voice_id is None:
            idx = zlib.crc32(first_text.encode("utf-8")) % len(self._voice_ids)
            self._voice_id = self._voice_ids[idx]
            log.info("tts.supertone.voice_picked", voice_index=idx, of=len(self._voice_ids))
        return self._voice_id

    def synthesize(self, text: str) -> tuple[str, float]:
        """한국어 텍스트를 Supertone TTS로 합성해 (WAV 경로, 길이초)를 반환한다.

        길이는 X-Audio-Length 헤더에서 읽고, 없으면 WAV 헤더 파싱으로 폴백한다.
        일시 오류(429/5xx)는 최대 3회 backoff 재시도한다.
        """
        clean = _sanitize_prompt_text(text.strip() or "", _MAX_TTS_CHARS)
        if not clean:
            raise VideoRenderError("Supertone TTS 입력 텍스트가 비어 있습니다")

        voice_id = self._pick_voice(clean)
        body = {
            "text": clean,
            "language": "ko",
            "model": self._model,
            "output_format": "wav",
        }
        url = f"{_SUPERTONE_TTS_BASE}/{voice_id}"

        wav_bytes, header_len = self._post_and_get_wav(url, body)
        out_path = Path(self.settings.nutti_media_dir) / f"st_voice_{uuid4().hex[:12]}.wav"
        _write_bytes(out_path, wav_bytes, "Supertone TTS 음성")

        duration = header_len if header_len is not None else _wav_duration_sec(wav_bytes)
        log.info("tts.supertone.saved", path=str(out_path), duration=round(duration, 2))
        return str(out_path), duration

    def _post_and_get_wav(self, url: str, body: dict) -> tuple[bytes, float | None]:
        """POST 요청을 보내고 (WAV 바이트, X-Audio-Length 초 또는 None)을 반환한다.

        성공 시 200 + 바이너리 오디오. 오류 본문은 노출하지 않는다(redaction).
        402(크레딧 소진)는 충전 안내를 덧붙여 즉시 실패한다(재시도 무의미).
        """
        attempts = 0
        while True:
            resp = _safe_send(
                lambda: self._client().post(
                    url,
                    headers=_supertone_headers(self.settings),
                    json=body,
                ),
                "Supertone TTS 합성",
            )
            code = getattr(resp, "status_code", None)
            if not isinstance(code, int):
                raise VideoRenderError("Supertone TTS 응답에 유효한 status_code가 없습니다")

            # 일시 오류(429 레이트 리밋 / 5xx 백엔드 장애) — 지수 backoff 재시도
            transient = code == 429 or code >= 500
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                wait = _RETRY_BACKOFF_SEC * (2 ** (attempts - 1))
                self._sleep(wait)
                continue

            if code == 402:
                # Supertone 문서: 크레딧 부족 시 402 — 시간이 지나도 안 풀리므로 안내를 명시.
                raise VideoRenderError(
                    "Supertone TTS 합성 HTTP 402 — 크레딧 소진(콘솔에서 충전 필요)"
                )
            if code >= 400:
                raise VideoRenderError(f"Supertone TTS 합성 HTTP {code}")

            content = getattr(resp, "content", None)
            if not isinstance(content, (bytes, bytearray)) or not content:
                raise VideoRenderError("Supertone TTS 응답에 오디오 바이트가 없습니다")

            # X-Audio-Length: 생성 오디오 길이(초). 없거나 못 읽으면 None(WAV 파싱 폴백).
            header_len: float | None = None
            headers = getattr(resp, "headers", None) or {}
            raw = headers.get("X-Audio-Length") or headers.get("x-audio-length")
            if raw is not None:
                try:
                    parsed = float(raw)
                    if parsed > 0:
                        header_len = parsed
                except (TypeError, ValueError):
                    header_len = None
            return bytes(content), header_len
