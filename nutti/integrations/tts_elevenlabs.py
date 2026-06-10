"""ElevenLabs TTS 클라이언트 — 한국어 아이 목소리 음성 합성.

video_kling.GeminiTtsClient와 동일한 공개 계약을 따른다:
  synthesize(text) -> (audio_path: str, duration_sec: float)

흐름: POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
  ?output_format=pcm_24000 으로 raw PCM 바이트를 받아 WAV로 감싸
  media_dir에 저장하고 (경로, 길이초)를 반환한다.

길이 계산: pcm_24000 = 16-bit mono S16LE 24kHz
  duration_sec = len(pcm_bytes) / (24000 * 2)   — 메타 파싱 불필요.

계약(video.py / video_kling.py와 동일 원칙):
- 모든 오류는 VideoRenderError로만 전파(상태 코드·예외 타입명만 노출,
  URL·응답 본문·요청 ID 미노출 — redaction).
- xi-api-key 헤더는 api.elevenlabs.io 요청에만 첨부(CDN 유출 방지).
- voice_id·model_id 는 URL/본문 삽입 전 정규식 검증(주입 표면 한정).
- _HttpClosingMixin으로 연결 풀 정리, _write_bytes 원자적 저장.
- 일시 오류(429/5xx)는 지수 backoff로 최대 3회 재시도.
- dry_run 게이트는 상위 백엔드(VideoStudio)가 담당 — 이 클라이언트는
  실 경로에서만 생성된다.
"""

from __future__ import annotations

import time
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
from nutti.integrations.video_kling import _pcm_to_wav_bytes
from nutti.logging import get_logger

log = get_logger(__name__)

# ElevenLabs TTS 엔드포인트. 자격증명(xi-api-key)은 이 호스트 요청에만 붙인다.
_ELEVENLABS_TTS_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
# 출력 포맷: raw 16-bit mono PCM 24kHz — 길이 계산이 바이트 수 나누기로 가능해 파싱 불필요.
_OUTPUT_FORMAT = "pcm_24000"
_PCM_SAMPLE_RATE = 24000  # Hz
_PCM_BYTES_PER_SAMPLE = 2  # 16-bit

# voice_id 허용 형태: 영숫자·`-` 1~128자. ElevenLabs 프리메이드 voice_id는 영숫자 20자이나,
# 사용자 생성/클론 voice_id는 UUIDv4 꼴(예: 550e8400-e29b-41d4-a716-446655440000)로 하이픈을
# 포함한다. 하이픈은 URL 경로를 변조하지 않으므로 허용한다(`/`, `?`, `:`, 공백 등 구조 변조
# 문자는 여전히 거부). 길이 상한 128은 UUID(36자)와 프리메이드(20자)를 모두 포괄한다.
import re  # noqa: E402 (stdlib, 모듈 상단이지만 상수 그룹 직후 배치)

_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")
_MAX_VOICE_ID_CHARS = 128

# model_id 허용 형태: 영숫자·`_`만 허용(ElevenLabs 공식 모델 ID 패턴).
# 예: eleven_multilingual_v2, eleven_flash_v2_5
_MODEL_ID_RE = re.compile(r"^[a-z0-9_]+$")
_MAX_MODEL_ID_CHARS = 64

# 내레이션 TTS 입력 길이 상한(과금·주입 표면 제한). 비트 1개 대사는 짧다.
_MAX_TTS_CHARS = 800

# 폴링 중 일시 오류(429/5xx) 최대 재시도 횟수와 backoff 기준(초). video_kling과 동일.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_SEC = 2.0


def _validate_voice_id(voice_id: str) -> str:
    """voice_id를 URL 경로에 삽입하기 전에 형식을 검증한다.

    ElevenLabs voice_id는 영숫자로만 이루어진 식별자다. `:`, `?`, `/`, 공백 등
    URL 구조를 변조할 문자가 들어가면 엔드포인트가 바뀔 수 있으므로 거부한다.
    실패는 VideoRenderError(설정 환경변수 이름만 노출 — 값 미노출).
    """
    vid = (voice_id or "").strip()
    if not vid or len(vid) > _MAX_VOICE_ID_CHARS or not _VOICE_ID_RE.match(vid):
        raise VideoRenderError(
            f"ElevenLabs voice_id(NUTTI_ELEVENLABS_VOICE_ID) 형식이 올바르지 않습니다 "
            f"(길이 {len(voice_id or '')})"
        )
    return vid


def _validate_elevenlabs_model_id(model_id: str) -> str:
    """model_id를 요청 본문에 삽입하기 전에 형식을 검증한다.

    ElevenLabs 모델 ID는 소문자 영숫자와 `_`로만 구성된다(예: eleven_multilingual_v2).
    그 외 문자(`:`, `?`, 공백 등)가 들어가면 요청 본문이 변조될 수 있으므로 거부한다.
    실패는 VideoRenderError(설정 환경변수 이름만 노출).
    """
    mid = (model_id or "").strip()
    if not mid or len(mid) > _MAX_MODEL_ID_CHARS or not _MODEL_ID_RE.match(mid):
        raise VideoRenderError(
            "ElevenLabs model_id(NUTTI_ELEVENLABS_MODEL) 형식이 올바르지 않습니다"
        )
    return mid


def _elevenlabs_headers(settings: Settings) -> dict:
    """ElevenLabs TTS 인증 헤더(`xi-api-key`). api.elevenlabs.io 요청에만 첨부한다.

    이 헤더는 자격증명이므로 **api.elevenlabs.io 요청에만** 사용한다 — CDN 등
    다른 호스트에 키를 실으면 그 호스트 로그/중간자에게 키가 샌다(Gemini 키 격리와 동일 원칙).
    """
    return {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
    }


class ElevenLabsTtsClient(_HttpClosingMixin):
    """ElevenLabs REST TTS로 한국어 아이 목소리 음성을 합성하는 클라이언트.

    `POST /v1/text-to-speech/{voice_id}?output_format=pcm_24000` 에 텍스트를 보내고
    raw PCM 바이트를 받아 WAV로 감싸 media_dir에 저장한 뒤 (경로, 길이초)를 반환한다.

    GeminiTtsClient와 동일한 공개 계약을 따른다:
      synthesize(text: str) -> tuple[str, float]  # (wav_path, duration_sec)

    모든 오류는 VideoRenderError로 전파(상태 코드·예외 타입명만 노출).
    테스트는 http= 인자로 fake HTTP 클라이언트를 주입해 네트워크 없이 검증한다.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep if sleep is not None else time.sleep
        # voice_id·model_id는 URL/본문에 삽입되므로 생성 시점에 형식 검증한다.
        # 잘못된 설정값이 실제 요청을 보낼 때까지 발견되지 않는 것을 방지한다.
        self._voice_id = _validate_voice_id(settings.elevenlabs_voice_id)
        self._model_id = _validate_elevenlabs_model_id(settings.elevenlabs_model_id)

    def _client(self):
        """httpx.Client를 지연 생성한다(주입된 fake가 있으면 그대로 반환)."""
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    def synthesize(self, text: str) -> tuple[str, float]:
        """한국어 텍스트를 ElevenLabs TTS로 합성해 (WAV 경로, 길이초)를 반환한다.

        PCM 포맷(pcm_24000)으로 요청해 바이트 수 나누기로 길이를 바로 계산한다.
        일시 오류(429/5xx)는 최대 3회 backoff 재시도한다.
        """
        clean = _sanitize_prompt_text(text.strip() or "", _MAX_TTS_CHARS)
        if not clean:
            raise VideoRenderError("ElevenLabs TTS 입력 텍스트가 비어 있습니다")

        body = {
            "text": clean,
            "model_id": self._model_id,
            "language_code": "ko",
            "voice_settings": {
                "stability": 0.35,
                "similarity_boost": 0.75,
                "style": 0.45,
                "use_speaker_boost": True,
                "speed": 1.05,
            },
        }
        url = f"{_ELEVENLABS_TTS_BASE}/{self._voice_id}"
        params = {"output_format": _OUTPUT_FORMAT}

        pcm = self._post_and_get_pcm(url, params, body)
        wav = _pcm_to_wav_bytes(pcm, _PCM_SAMPLE_RATE)
        out_path = Path(self.settings.nutti_media_dir) / f"el_voice_{uuid4().hex[:12]}.wav"
        _write_bytes(out_path, wav, "ElevenLabs TTS 음성")

        # 길이(초) = 총 샘플수 / 샘플레이트 = (PCM 바이트 / 2바이트per샘플) / rate
        duration = (len(pcm) / _PCM_BYTES_PER_SAMPLE) / _PCM_SAMPLE_RATE
        log.info("tts.elevenlabs.saved", path=str(out_path), duration=round(duration, 2))
        return str(out_path), duration

    def _post_and_get_pcm(self, url: str, params: dict, body: dict) -> bytes:
        """POST 요청을 보내고 응답 바이너리(PCM)를 반환한다. 일시 오류는 재시도한다.

        ElevenLabs는 성공 시 200 + application/octet-stream(raw 바이너리)을 반환한다.
        Telegram과 달리 'ok: false' 패턴이 없으며, HTTP 상태 코드가 직접 실패를 표현한다.
        오류 응답(4xx/5xx)은 JSON일 수 있지만 본문 내용은 노출하지 않는다(redaction).
        """
        attempts = 0
        while True:
            resp = _safe_send(
                lambda: self._client().post(
                    url,
                    headers=_elevenlabs_headers(self.settings),
                    params=params,
                    json=body,
                ),
                "ElevenLabs TTS 합성",
            )
            code = getattr(resp, "status_code", None)
            if not isinstance(code, int):
                raise VideoRenderError("ElevenLabs TTS 응답에 유효한 status_code가 없습니다")

            # 일시 오류(429 쿼터 초과 / 5xx 백엔드 장애) — 지수 backoff 재시도
            transient = code == 429 or code >= 500
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                wait = _RETRY_BACKOFF_SEC * (2 ** (attempts - 1))
                self._sleep(wait)
                continue

            if code >= 400:
                raise VideoRenderError(f"ElevenLabs TTS 합성 HTTP {code}")

            # 200: raw PCM 바이너리
            content = getattr(resp, "content", None)
            if not isinstance(content, (bytes, bytearray)) or not content:
                raise VideoRenderError("ElevenLabs TTS 응답에 오디오 바이트가 없습니다")
            return bytes(content)
