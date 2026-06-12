"""Kling 무음 영상 + 한국어 TTS 보이스오버 백엔드.

Veo는 네이티브 한국어 음성을 내지만 품질이 실측 미달이고, Kling은 더 저렴하나
**한국어 음성을 못 낸다**(v3는 한국어를 영어로 자동번역, v1.6/2.1은 아예 무음).
그래서 이 백엔드는 ① Kling으로 **무음** image-to-video 클립을 비트별로 만들고,
② Gemini TTS로 한국어 내레이션을 합성한 뒤, ③ ffmpeg로 음성을 무음 영상에
입혀(mux) 비트 클립을 만든다. 마스코트는 입을 맞추지 않고(립싱크 포기) 내레이션
위에서 자연스럽게 움직인다(보이스오버 포맷).

흐름(비트별): TTS(한국어 PCM→WAV) → Kling 무음 클립(길이=내레이션에 맞춘 5/10초)
  → mux(무음 영상 + WAV, `-shortest`) → 비트 클립. 비트들은 VideoStudio._stitch가 잇는다.

계약(video.py와 동일):
- 모든 오류는 `VideoRenderError`(폴링 초과는 `VideoTimeoutError`)로만 전파.
- redaction: 메시지에 URL·request id·응답 본문 금지(상태 코드·예외 타입명만).
- 주입 가능(http=/sleep=)으로 네트워크 없이 테스트.
- `_HttpClosingMixin`으로 연결 풀 정리.
- `_write_bytes` 원자적 저장.
- API 응답값(request id·영상 URL)은 신뢰 불가 입력 → 형식·호스트 검증(SSRF 방어).
- dry_run 게이트는 상위 VideoStudio가 담당(여기 클라이언트는 실 경로에서만 생성).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from nutti.config import Settings
from nutti.integrations.video import (
    VideoRenderError,
    VideoTimeoutError,
    _HttpClosingMixin,
    _close_owned,
    _guess_image_mime,
    _json_or_raise,
    _raise_for_status,
    _read_bytes,
    _safe_send,
    _sanitize_prompt_text,
    _send_json,
    _write_bytes,
)
from nutti.logging import get_logger

log = get_logger(__name__)

# Gemini API 베이스(이미지/Veo와 동일 호스트·인증). TTS도 generateContent를 쓴다.
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# fal.ai 큐 API 베이스. 제출/상태/결과 모두 이 호스트(자격증명 헤더는 여기에만 붙인다).
_FAL_QUEUE_BASE = "https://queue.fal.run"
# 결과 영상 다운로드를 허용하는 fal CDN 호스트(신뢰 불가 응답 URL의 SSRF 방어).
_FAL_SAFE_HOSTS = frozenset({"fal.media", "fal.run"})
# fal request id 허용 형태(폴링 URL에 삽입 전 검증). 영숫자·`-`·`_`만 허용.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_REQUEST_ID_CHARS = 128
# Kling 모델 경로(설정값) 허용 형태. fal 모델 id는 `fal-ai/kling-video/.../image-to-video` 꼴.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_MODEL_ID_CHARS = 256
# TTS 사전구성 음성 이름(설정값) 허용 형태. Gemini 음성은 `Kore`·`Puck` 등 단순 식별자라
# JSON voiceName에 넣을 때 영숫자·공백·`-`·`_`만 허용한다(주입 표면 한정).
_TTS_VOICE_RE = re.compile(r"^[A-Za-z0-9 _-]+$")
_MAX_TTS_VOICE_CHARS = 64
# 프롬프트에 삽입하는 AI 생성 텍스트 길이 상한(주입 표면 제한).
_MAX_SCENE_CHARS = 500
# 내레이션 TTS 입력 길이 상한(비트 1개 대사는 짧다 — 과금·주입 표면 한정).
_MAX_TTS_CHARS = 800
# 폴링 중 일시 오류(429/5xx) 최대 재시도와 backoff 기준(초). video.py와 동일 원칙.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_SEC = 2.0
# Kling standard(v1.6/2.1)가 허용하는 클립 길이(초). 내레이션 길이를 이 중 하나로 올림.
_KLING_ALLOWED_DURATIONS = (5, 10)
# LipSync 입력 길이 가드(초). fal.ai 문서: 영상 2~10초, 음성 2~60초. 위반 시 서버가
# 거부하므로 제출 전 클라이언트에서 명확한 오류로 막아 불필요한 과금·왕복을 피한다.
_LIPSYNC_MIN_VIDEO_SEC = 2.0
_LIPSYNC_MAX_VIDEO_SEC = 10.0
_LIPSYNC_MIN_AUDIO_SEC = 2.0
_LIPSYNC_MAX_AUDIO_SEC = 60.0
# TTS mimeType에 rate가 없을 때의 기본 샘플레이트(Gemini TTS는 24kHz mono 16-bit PCM).
_DEFAULT_TTS_RATE = 24000


def _unlink_quiet(path: str | None) -> None:
    """파일을 조용히 삭제한다(경로 없음·미존재·OSError 모두 무시).

    비트 루프의 중간 산출물(무음 영상·내레이션 WAV)과 중도 실패 시 이미 완성된
    beat_*.mp4를 정리하는 용도. 정리 자체가 실패해도 원래 흐름/예외를 막지 않는다.
    """
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _fal_headers(settings: Settings) -> dict:
    """fal.ai 인증 헤더(`Authorization: Key <FAL_KEY>`). 큐 호스트 요청에만 붙인다.

    이 헤더는 자격증명이므로 **queue.fal.run 요청에만** 사용한다 — 결과 영상은
    fal CDN(fal.media)에서 키 없이 내려받으므로, CDN 요청에 키를 실으면 그 호스트의
    로그/중간자에게 키가 샌다(Gemini 키 격리와 동일 원칙).
    """
    return {"Authorization": f"Key {settings.fal_key}", "Content-Type": "application/json"}


def _gemini_headers(settings: Settings) -> dict:
    """Gemini TTS 인증 헤더(`x-goog-api-key`). Gemini 도메인 요청에만 붙인다."""
    return {"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"}


def _validate_request_id(request_id: str) -> str:
    """fal request id가 폴링 URL에 안전하게 삽입 가능한 형태인지 검증한다.

    제출 응답의 request_id를 `{base}/{model}/requests/{id}/status` 등으로 이어
    붙이므로 신뢰 불가 입력으로 본다. 허용 문자(영숫자·`-`·`_`)·길이만 통과.
    """
    rid = (request_id or "").strip()
    if not rid or len(rid) > _MAX_REQUEST_ID_CHARS or not _REQUEST_ID_RE.match(rid):
        raise VideoRenderError(f"Kling request id 형식이 올바르지 않습니다 (길이 {len(request_id or '')})")
    return rid


def _validate_model_id(model_id: str, *, env_name: str = "NUTTI_KLING_MODEL") -> str:
    """모델 경로(설정값)를 URL에 삽입하기 전에 형식을 검증한다.

    설정값이라 신뢰도는 높지만, 오설정으로 `:`·`?`·공백 등이 들어가면 요청 대상이
    변조될 수 있으므로 허용 문자(영숫자·`.`·`_`·`/`·`-`)·길이만 통과시킨다.
    Kling 모델(NUTTI_KLING_MODEL)과 TTS 모델(NUTTI_TTS_MODEL)이 같은 검증을
    공유하므로, 오류 메시지에 어느 설정인지 `env_name`으로 구분해 노출한다.
    """
    mid = (model_id or "").strip().strip("/")
    if not mid or len(mid) > _MAX_MODEL_ID_CHARS or not _MODEL_ID_RE.match(mid):
        raise VideoRenderError(f"모델 id({env_name}) 형식이 올바르지 않습니다")
    return mid


def _validate_tts_voice(voice: str) -> str:
    """TTS 음성 이름(설정값)을 JSON voiceName에 넣기 전에 형식을 검증한다.

    _validate_model_id와 동일 원칙: 설정값이라도 오설정으로 제어문자·따옴표·
    개행 등이 들어가면 요청 본문이 변조될 수 있으므로 허용 문자(영숫자·공백·
    `-`·`_`)·길이만 통과시킨다. 실패는 다른 검증과 동일하게 VideoRenderError.
    """
    v = (voice or "").strip()
    if not v or len(v) > _MAX_TTS_VOICE_CHARS or not _TTS_VOICE_RE.match(v):
        raise VideoRenderError("TTS 음성 이름(NUTTI_TTS_VOICE) 형식이 올바르지 않습니다")
    return v


def _validate_fal_video_url(url: str) -> None:
    """결과 영상 다운로드 URL이 허용된 fal CDN 호스트인지 검증한다(SSRF 방어).

    scheme=https + host가 _FAL_SAFE_HOSTS(또는 그 서브도메인)여야 한다.
    API 응답값(영상 URL)은 신뢰 불가 입력이므로 다운로드 전에 검증한다.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise VideoRenderError("Kling 다운로드: 영상 URL scheme 불허 (허용: https)")
    host = (parsed.hostname or "").lower()
    if not any(host == s or host.endswith(f".{s}") for s in _FAL_SAFE_HOSTS):
        raise VideoRenderError("Kling 다운로드: 영상 URL 호스트 불허 (허용: fal.media, fal.run)")


def _pick_clip_duration(audio_sec: float) -> int:
    """내레이션 길이(초)를 Kling이 허용하는 클립 길이(5/10초)로 올림한다.

    내레이션보다 짧은 클립을 만들면 음성이 잘리므로, 음성 길이 이상인 가장 짧은
    허용 길이를 고른다. 음성이 최대 허용(10초)을 넘으면 mux `-shortest`가 내레이션
    뒷부분을 소리 없이 잘라낸다(2026-06-11 실측: 비트가 10초 캡에서 잘림) —
    silent 잘림 대신 Kling 호출(과금) 전에 명시 에러로 막는다. 해결책은 대본
    비트를 더 짧게(프롬프트 상한 45자) 재생성하는 것.
    """
    for d in _KLING_ALLOWED_DURATIONS:
        if audio_sec <= d:
            return d
    raise VideoRenderError(
        f"내레이션({audio_sec:.1f}초)이 Kling 클립 최대 길이"
        f"({_KLING_ALLOWED_DURATIONS[-1]}초)를 초과합니다 — 그대로 진행하면 음성이"
        " 소리 없이 잘립니다. 대본 비트를 45자 이내로 줄여 다시 생성하세요."
    )


def _guess_video_mime(path: str) -> str:
    """확장자로 영상 MIME 타입을 추정한다(.mov → video/quicktime, 그 외 mp4).

    LipSync 제출 시 무음 클립을 base64 data URI(`data:{mime};base64,...`)로 보낼 때
    쓴다. fal.ai LipSync는 mp4/mov 영상을 받으므로 두 가지만 구분한다.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".mov":
        return "video/quicktime"
    return "video/mp4"


def _guess_audio_mime(path: str) -> str:
    """확장자로 음성 MIME 타입을 추정한다(.mp3/.ogg/.m4a/.aac/그 외 → wav).

    LipSync 제출 시 TTS 음성을 base64 data URI로 보낼 때 쓴다. TTS는 WAV를
    내므로 기본값은 audio/wav지만, ElevenLabs(mp3) 등 다른 소스도 대비한다.
    """
    suffix = Path(path).suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
    }.get(suffix, "audio/wav")


def _pcm_to_wav_bytes(pcm: bytes, rate: int) -> bytes:
    """16-bit mono PCM 바이트를 WAV 컨테이너로 감싼다(ffmpeg가 바로 읽도록).

    Gemini TTS는 헤더 없는 raw PCM(L16, signed 16-bit LE, mono)을 반환하므로,
    `wave` 모듈로 표준 WAV 헤더를 붙인다.
    """
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


class KlingPromptBuilder:
    """Kling 무음 클립용 프롬프트 빌더(말하는 마스코트 + 별도 TTS 내레이션).

    영상 자체는 무음이고 음성은 별도 TTS를 mux한다. 마스코트는 카메라를 보며
    **말하는 연출**(입·턱 움직임 명시)을 한다 — Kling LipSync가 강아지 얼굴을
    인식하지 못해(2026-06-11 face_detection_error) 후처리 립싱크 대신 채택한 방식.
    음절 단위 동기화는 아니고 "말하는 분위기"까지가 한계(2026-06-12 PO 육안 승인).
    ① 화면에 글자/자막 금지 ② 추가 동물·사람 금지를 명시하고, 비트 텍스트
    (내레이션 내용)는 장면 분위기 힌트로만 쓰며 `_sanitize_prompt_text`로 정제한다.
    """

    # =========================== PO 수정 구역 (Kling 연출) ===========================
    # Kling 무음 영상의 "움직임·카메라·금지요소"를 바꾸려면 아래 영어 템플릿을 고친다.
    # 마스코트는 말하는 연출로 움직이고 음성은 TTS mux — _TALKING을 빼면 입이 안 움직인다.
    # 2026-06-12 실테스트 PO 피드백("말하는 분위기가 아니다")으로 강화: 입 움직임을
    # 클립 전체에 걸친 '가장 중요한 단일 요구사항'으로 반복 명시하고, 닫힌 입을 금지한다.
    _MOTION = (
        "A photorealistic dog mascot in a cozy warmly lit studio, facing the camera and "
        "speaking non-stop directly to the viewer like an energetic TV show host delivering "
        "lines, very expressive animated face, lively eyebrows and ears, small emphatic head "
        "nods on key words."
    )
    _CAMERA = (
        "Camera: locked-off tripod, medium close-up framing the face so the mouth is large "
        "and clearly visible, eye-level, no camera movement."
    )
    _TALKING = (
        "Most important requirement: the dog's mouth is clearly moving in continuous speech "
        "for the entire duration of the clip — jaw opening and closing repeatedly, visible "
        "lip articulation, like an actively talking news anchor. The mouth must never stay "
        "closed or resting (narration audio is added separately; approximate sync is fine)."
    )
    _NEGATIVE = (
        "Strictly no additional animals, no people. Absolutely no text, subtitles, captions, "
        "letters, words, or writing anywhere in the frame. The dog must not be silent, "
        "still, or merely smiling with a closed mouth."
    )
    # ========================= PO 수정 구역 끝 (Kling 연출) =========================

    def build_beat(self, beat_text: str) -> str:
        """비트 1개의 무음 Kling 프롬프트를 만든다(말하는 마스코트 + 자막 금지).

        beat_text는 내레이션 내용이라 그대로 발화시키지 않고 장면 톤 힌트로만 둔다 —
        실제로는 _MOTION·_TALKING의 고정 연출이 주가 되고, 정제된 비트는 짧은 무드 큐로 붙는다.
        """
        mood = _sanitize_prompt_text(beat_text.strip() or "", _MAX_SCENE_CHARS)
        return (
            f"{self._MOTION} Scene mood (do not render as text): '{mood}'. "
            f"{self._CAMERA} {self._TALKING} "
            "Format: vertical 9:16. "
            f"{self._NEGATIVE}"
        )


class GeminiTtsClient(_HttpClosingMixin):
    """Gemini TTS로 한국어 내레이션을 합성하는 클라이언트(기존 GEMINI_API_KEY 재사용).

    `POST /models/{tts_model}:generateContent`에 responseModalities=["AUDIO"]와
    speechConfig(사전구성 음성)를 보내고, 응답의 오디오 파트(base64 PCM)를 디코드해
    WAV로 감싸 media_dir에 저장한 뒤 (경로, 길이초)를 반환한다. 모든 오류는
    VideoRenderError로 전파(상태 코드·타입명만 노출). 테스트는 http=로 fake 주입.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep if sleep is not None else time.sleep
        # 설정값(tts_model)은 URL에, tts_voice는 JSON 본문에 삽입되므로 신뢰 불가
        # 입력처럼 형식을 검증해 둔다(KlingClient가 kling_model을 검증하는 것과 동일).
        self._tts_model = _validate_model_id(settings.tts_model, env_name="NUTTI_TTS_MODEL")
        self._tts_voice = _validate_tts_voice(settings.tts_voice)

    def _client(self):
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    def synthesize(self, text: str) -> tuple[str, float]:
        """한국어 텍스트를 음성으로 합성해 WAV 경로와 길이(초)를 반환한다."""
        clean = _sanitize_prompt_text(text.strip() or "", _MAX_TTS_CHARS)
        if not clean:
            raise VideoRenderError("TTS 입력 텍스트가 비어 있습니다")
        body = {
            "contents": [{"parts": [{"text": clean}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": self._tts_voice}
                    }
                },
            },
        }
        url = f"{_GEMINI_BASE}/models/{self._tts_model}:generateContent"
        data = _send_json(
            lambda: self._client().post(url, headers=_gemini_headers(self.settings), json=body),
            "Gemini TTS 합성",
            sleep=self._sleep,
            max_transient_retries=_MAX_TRANSIENT_RETRIES,
        )
        pcm, rate = self._extract_audio(data)
        wav = _pcm_to_wav_bytes(pcm, rate)
        out_path = Path(self.settings.nutti_media_dir) / f"voice_{uuid4().hex[:12]}.wav"
        _write_bytes(out_path, wav, "TTS 음성")
        # 길이(초) = 샘플수 / rate = (PCM 바이트 / 2) / rate.
        duration = (len(pcm) / 2) / rate if rate > 0 else 0.0
        log.info("tts.voice.saved", path=str(out_path), duration=round(duration, 2))
        return str(out_path), duration

    @staticmethod
    def _extract_audio(data: dict) -> tuple[bytes, int]:
        """generateContent 응답에서 첫 오디오 파트의 PCM 바이트와 샘플레이트를 추출한다.

        NanoBananaClient._extract_image_bytes와 동일한 방어 패턴(순회·isinstance,
        snake/camelCase 둘 다 허용). mimeType의 `rate=` 파라미터로 샘플레이트를
        파싱하고, 없으면 기본값(24000)을 쓴다. 오디오 파트가 없으면 명시적으로 실패.
        """
        import base64
        import binascii

        candidates = data.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                content = candidate.get("content")
                parts = content.get("parts") if isinstance(content, dict) else None
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    inline = part.get("inline_data") or part.get("inlineData")
                    if not isinstance(inline, dict):
                        continue
                    encoded = inline.get("data")
                    if not encoded:
                        continue
                    mime = inline.get("mime_type") or inline.get("mimeType") or ""
                    rate = _parse_pcm_rate(mime)
                    try:
                        return base64.b64decode(encoded), rate
                    except (binascii.Error, ValueError) as exc:
                        raise VideoRenderError(
                            f"Gemini TTS base64 디코드 실패: {type(exc).__name__}"
                        ) from None
        finish_reason = None
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            finish_reason = candidates[0].get("finishReason")
        log.debug("tts.missing_audio", keys=list(data.keys()), finish_reason=finish_reason)
        if finish_reason:
            raise VideoRenderError(f"Gemini TTS 오디오 파트 없음 — finishReason={finish_reason}")
        raise VideoRenderError(f"Gemini TTS 응답에 오디오 파트가 없습니다 (응답 키: {list(data.keys())})")


def _parse_pcm_rate(mime: str) -> int:
    """오디오 mimeType(예: 'audio/L16;codec=pcm;rate=24000')에서 rate를 파싱한다."""
    if not isinstance(mime, str):
        return _DEFAULT_TTS_RATE
    m = re.search(r"rate=(\d+)", mime)
    if m:
        try:
            rate = int(m.group(1))
            if rate > 0:
                return rate
        except ValueError:
            pass
    return _DEFAULT_TTS_RATE


class KlingClient(_HttpClosingMixin):
    """fal.ai Kling image-to-video(무음) 클라이언트(제출 → 폴링 → 즉시 다운로드).

    fal 큐 REST: ① `POST {base}/{model}`로 제출(시작 프레임 data URI·프롬프트·길이) →
    ② request_id를 받아 `GET {base}/{model}/requests/{id}/status`를 interval·timeout
    한도까지 폴링 → ③ COMPLETED면 `GET {base}/{model}/requests/{id}`로 결과를 받아
    영상 URL을 검증·다운로드해 media_dir에 저장. 폴링/결과 URL은 응답값을 쓰지 않고
    request_id로 **직접 구성**한다(신뢰 불가 응답 URL의 SSRF 차단).

    오류 계약: HTTP·전송·JSON·쓰기 실패는 VideoRenderError, 폴링 초과는
    VideoTimeoutError. 일시 오류(429/5xx)는 backoff로 최대 3회 재시도.
    자격증명(Authorization)은 queue.fal.run 요청에만, CDN 다운로드엔 붙이지 않는다.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep if sleep is not None else time.sleep
        self._model = _validate_model_id(settings.kling_model)
        # fal 큐의 status/result 조회는 **앱 ID(앞 2세그먼트, 예: "fal-ai/kling-video")**만 쓴다.
        # 전체 모델 경로(".../v2.1/standard/image-to-video")는 제출(POST)에만 유효하고,
        # status/result(GET)에 그대로 붙이면 405(Method Not Allowed)가 난다 — fal 큐는
        # 작업을 앱 단위로 추적하기 때문이다. 검증된 model_id에서 앞 2세그먼트만 떼어 구성한다.
        _segs = self._model.split("/")
        self._app_id = "/".join(_segs[:2]) if len(_segs) >= 2 else self._model
        self._interval = float(settings.kling_poll_interval_sec)
        if self._interval <= 0:
            raise ValueError(f"kling_poll_interval_sec는 0보다 커야 합니다(현재 {self._interval})")
        self._timeout = float(settings.kling_timeout_sec)
        if self._timeout <= 0:
            raise ValueError(f"kling_timeout_sec는 0보다 커야 합니다(현재 {self._timeout})")
        self.poll_count = 0

    def _client(self):
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    def generate(self, frame_path: str, prompt: str, duration_sec: int) -> str:
        """시작 프레임 + 프롬프트 + 길이로 무음 영상을 생성하고 로컬 저장 경로를 반환한다."""
        request_id = self._submit(frame_path, prompt, duration_sec)
        video_url = self._poll(request_id)
        return self._download(video_url)

    def _submit(self, frame_path: str, prompt: str, duration_sec: int) -> str:
        """image-to-video 작업을 제출하고 검증된 request_id를 반환한다.

        시작 프레임은 base64 data URI로 보낸다(fal은 image_url에 URL 또는 data URI 허용).
        """
        import base64

        frame_bytes = _read_bytes(frame_path, "Kling 시작 프레임")
        mime = _guess_image_mime(frame_path)
        data_uri = f"data:{mime};base64,{base64.b64encode(frame_bytes).decode('ascii')}"
        body = {
            "image_url": data_uri,
            "prompt": prompt,
            "duration": str(duration_sec),
            "aspect_ratio": "9:16",
        }
        url = f"{_FAL_QUEUE_BASE}/{self._model}"
        data = _send_json(
            lambda: self._client().post(url, headers=_fal_headers(self.settings), json=body),
            "Kling 작업 제출",
        )
        request_id = data.get("request_id")
        if not request_id:
            log.debug("kling.submit.missing_request_id", keys=list(data.keys()))
            raise VideoRenderError(f"Kling 응답에 request_id가 없습니다 (응답 키: {list(data.keys())})")
        return _validate_request_id(str(request_id))

    def _poll(self, request_id: str) -> str:
        """상태를 COMPLETED까지 폴링하고, 결과에서 검증된 영상 URL을 반환한다.

        경계는 `< timeout`(off-by-one 방지). 폴링/결과 URL은 request_id로 직접 구성한다.
        """
        status_url = f"{_FAL_QUEUE_BASE}/{self._app_id}/requests/{request_id}/status"
        elapsed = 0.0
        while elapsed < self._timeout:
            data, backoff_sec = self._status_once(status_url)
            elapsed += backoff_sec
            status = data.get("status")
            if status == "COMPLETED":
                return self._fetch_result_url(request_id)
            if status in ("IN_QUEUE", "IN_PROGRESS", None):
                self._sleep(self._interval)
                elapsed += self._interval
                continue
            # ERROR 등 종료 상태(미진행) — 명시적으로 실패한다(무한 폴링 방지).
            raise VideoRenderError(f"Kling 작업 실패: status={status}")
        raise VideoTimeoutError(f"Kling 폴링 타임아웃({self._timeout:.0f}s, 폴링 {self.poll_count}회)")

    def _status_once(self, url: str) -> tuple[dict, float]:
        """상태 1회 조회. 일시 오류(429/5xx)는 지수 backoff로 최대 3회 재시도.

        반환은 (응답 dict, 재시도 backoff 합계 초) — backoff는 호출부가 timeout에 누적.
        """
        attempts = 0
        backoff_total = 0.0
        while True:
            self.poll_count += 1
            resp = _safe_send(
                lambda: self._client().get(url, headers=_fal_headers(self.settings)),
                "Kling 상태 조회",
            )
            code = getattr(resp, "status_code", None)
            transient = isinstance(code, int) and (code == 429 or code >= 500)
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                wait = _RETRY_BACKOFF_SEC * (2 ** (attempts - 1))
                self._sleep(wait)
                backoff_total += wait
                continue
            return _json_or_raise(resp, "Kling 상태 조회"), backoff_total

    def _fetch_result_url(self, request_id: str) -> str:
        """완료된 작업의 결과를 받아 영상 URL을 방어적으로 추출·검증해 반환한다."""
        result_url = f"{_FAL_QUEUE_BASE}/{self._app_id}/requests/{request_id}"
        data = _send_json(
            lambda: self._client().get(result_url, headers=_fal_headers(self.settings)),
            "Kling 결과 조회",
        )
        video = data.get("video")
        uri = video.get("url") if isinstance(video, dict) else None
        if not uri:
            log.debug("kling.result.missing_url", keys=list(data.keys()))
            raise VideoRenderError(f"Kling 결과에 영상 URL이 없습니다 (응답 키: {list(data.keys())})")
        uri = str(uri)
        _validate_fal_video_url(uri)
        return uri

    def _download(self, uri: str) -> str:
        """검증된 영상 URL에서 바이트를 내려받아 media_dir에 저장하고 경로를 반환한다.

        CDN(fal.media)은 키 없이 내려받는다(자격증명 헤더 미첨부). 리다이렉트는
        한 hop만 허용하고 그 Location도 호스트를 재검증한다(SSRF 체인 차단).
        """
        _validate_fal_video_url(uri)
        resp = _safe_send(
            lambda: self._client().get(uri, follow_redirects=False),
            "Kling 영상 다운로드",
        )
        sc = getattr(resp, "status_code", None)
        if not isinstance(sc, int):
            raise VideoRenderError("Kling 영상 다운로드 응답에 유효한 status_code가 없습니다")
        if 300 <= sc < 400:
            location = (getattr(resp, "headers", {}) or {}).get("location", "")
            if not location:
                raise VideoRenderError("Kling 영상 다운로드: 리다이렉트 응답에 Location 헤더 없음")
            _validate_fal_video_url(location)
            resp = _safe_send(
                lambda: self._client().get(location, follow_redirects=False),
                "Kling 영상 다운로드(리다이렉트)",
            )
            r_sc = getattr(resp, "status_code", None)
            if isinstance(r_sc, int) and 300 <= r_sc < 400:
                raise VideoRenderError("Kling 다운로드: 허용 호스트 이후 추가 리다이렉트 금지")
        _raise_for_status(resp, "Kling 영상 다운로드")
        content = getattr(resp, "content", None)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise VideoRenderError("Kling 다운로드 응답에 영상 바이트가 없습니다")
        out_path = Path(self.settings.nutti_media_dir) / f"kling_{uuid4().hex[:12]}.mp4"
        _write_bytes(out_path, bytes(content), "Kling 영상")
        log.info("kling.video.saved", path=str(out_path))
        return str(out_path)


class KlingLipSyncClient(_HttpClosingMixin):
    """fal.ai Kling LipSync(audio-to-video) 클라이언트(무음 영상 + 음성 → 립싱크 영상).

    무음 Kling 클립과 TTS 음성을 받아, fal 큐 REST로 마스코트 입을 음성에 동기화한
    영상을 만든다. 흐름은 KlingClient와 동일: ① `POST {base}/{model}`로 제출
    (video_url·audio_url base64 data URI) → ② request_id 폴링
    (`GET {base}/{model}/requests/{id}/status`) → ③ COMPLETED면
    `GET {base}/{model}/requests/{id}`에서 영상 URL을 검증·다운로드.

    KlingClient와의 차이:
    - has_audio 신호 처리(아래)와 입력 형태(영상+음성 data URI)만 다르고, 폴링 URL은
      KlingClient와 동일하게 **앱 ID(앞 2세그먼트)**를 쓴다 — 당초 fal 공식 문서대로
      전체 모델 경로를 썼으나 라이브에서 405가 났다(2026-06-11 실측, PR #31과 동일 증상).
    - has_audio 신호: fal LipSync 응답에는 출력 MP4의 오디오 포함 여부 필드가 없다.
      모델 목적상 출력에는 항상 음성이 입혀진다고 보고 has_audio=True를 기본으로
      반환하되, 응답에서 음성 부재를 시사하는 신호가 잡히면 False로 폴백한다
      (백엔드가 안전하게 mux로 되돌릴 수 있도록). TODO(live): 라이브에서 출력
      MP4에 실제 오디오 트랙이 있는지 ffprobe로 1회 검증 후 이 가정을 확정한다.

    재사용(KlingClient와 동일 계약): _fal_headers(큐 호스트 한정 자격증명),
    _validate_request_id/_validate_model_id(URL 삽입 전 형식 검증),
    _validate_fal_video_url(SSRF 호스트 검증), _send_json/_safe_send/_json_or_raise,
    _read_bytes/_write_bytes, backoff 재시도. 오류 계약: HTTP/전송/JSON/쓰기 실패는
    VideoRenderError, 폴링 초과는 VideoTimeoutError, redaction(상태 코드·타입명만).
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep if sleep is not None else time.sleep
        self._model = _validate_model_id(
            settings.kling_lipsync_model, env_name="NUTTI_KLING_LIPSYNC_MODEL"
        )
        # status/result 조회는 앱 ID(앞 2세그먼트, "fal-ai/kling-video")만 쓴다.
        # fal 공식 문서는 GET {base}/{model}/requests/{id}/status라고 안내하지만,
        # 전체 모델 경로(.../lipsync/audio-to-video)를 붙이면 라이브에서 405가 난다
        # (2026-06-11 실측 — KlingClient가 PR #31에서 겪은 것과 동일한 fal 큐 동작).
        _segs = self._model.split("/")
        self._app_id = "/".join(_segs[:2]) if len(_segs) >= 2 else self._model
        self._interval = float(settings.kling_poll_interval_sec)
        if self._interval <= 0:
            raise ValueError(f"kling_poll_interval_sec는 0보다 커야 합니다(현재 {self._interval})")
        self._timeout = float(settings.kling_timeout_sec)
        if self._timeout <= 0:
            raise ValueError(f"kling_timeout_sec는 0보다 커야 합니다(현재 {self._timeout})")
        self.poll_count = 0

    def _client(self):
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    def generate(
        self,
        silent_video_path: str,
        audio_path: str,
        *,
        video_sec: float | None = None,
        audio_sec: float | None = None,
    ) -> tuple[str, bool]:
        """무음 영상 + 음성으로 립싱크 영상을 만들어 (로컬 경로, has_audio)를 반환한다.

        video_sec/audio_sec를 주면 제출 전 길이 제약(영상 2~10초·음성 2~60초)을
        검증한다(None이면 건너뜀 — 호출부가 이미 보장하는 경우). has_audio는 출력
        MP4에 음성이 입혀졌는지 신호다(True면 백엔드가 mux를 생략).
        """
        self._guard_durations(video_sec, audio_sec)
        request_id = self._submit(silent_video_path, audio_path)
        video_url, has_audio = self._poll(request_id)
        return self._download(video_url), has_audio

    @staticmethod
    def _guard_durations(video_sec: float | None, audio_sec: float | None) -> None:
        """제출 전 입력 길이가 fal LipSync 제약을 만족하는지 검증한다(과금·왕복 절약).

        영상 2~10초, 음성 2~60초. 위반 시 명확한 VideoRenderError로 즉시 막는다.
        값이 None이면 그 항목은 검사하지 않는다.
        """
        if video_sec is not None and not (
            _LIPSYNC_MIN_VIDEO_SEC <= video_sec <= _LIPSYNC_MAX_VIDEO_SEC
        ):
            raise VideoRenderError(
                f"LipSync 영상 길이 제약 위반(허용 {_LIPSYNC_MIN_VIDEO_SEC:.0f}~"
                f"{_LIPSYNC_MAX_VIDEO_SEC:.0f}초, 현재 {video_sec:.1f}초)"
            )
        if audio_sec is not None and not (
            _LIPSYNC_MIN_AUDIO_SEC <= audio_sec <= _LIPSYNC_MAX_AUDIO_SEC
        ):
            raise VideoRenderError(
                f"LipSync 음성 길이 제약 위반(허용 {_LIPSYNC_MIN_AUDIO_SEC:.0f}~"
                f"{_LIPSYNC_MAX_AUDIO_SEC:.0f}초, 현재 {audio_sec:.1f}초)"
            )

    def _submit(self, silent_video_path: str, audio_path: str) -> str:
        """LipSync 작업을 제출하고 검증된 request_id를 반환한다.

        무음 영상·음성은 base64 data URI(`data:{mime};base64,...`)로 보낸다 — fal는
        video_url/audio_url에 원격 URL과 data URI를 모두 허용한다(로컬 파일은 별도
        업로드 없이 data URI가 간편). 필드명은 video/audio가 아니라 video_url/audio_url.
        """
        import base64

        video_bytes = _read_bytes(silent_video_path, "LipSync 무음 영상")
        audio_bytes = _read_bytes(audio_path, "LipSync 음성")
        video_mime = _guess_video_mime(silent_video_path)
        audio_mime = _guess_audio_mime(audio_path)
        video_uri = f"data:{video_mime};base64,{base64.b64encode(video_bytes).decode('ascii')}"
        audio_uri = f"data:{audio_mime};base64,{base64.b64encode(audio_bytes).decode('ascii')}"
        body = {"video_url": video_uri, "audio_url": audio_uri}
        url = f"{_FAL_QUEUE_BASE}/{self._model}"
        data = _send_json(
            lambda: self._client().post(url, headers=_fal_headers(self.settings), json=body),
            "LipSync 작업 제출",
        )
        request_id = data.get("request_id")
        if not request_id:
            log.debug("lipsync.submit.missing_request_id", keys=list(data.keys()))
            raise VideoRenderError(
                f"LipSync 응답에 request_id가 없습니다 (응답 키: {list(data.keys())})"
            )
        return _validate_request_id(str(request_id))

    def _poll(self, request_id: str) -> tuple[str, bool]:
        """상태를 COMPLETED까지 폴링하고 (검증된 영상 URL, has_audio)를 반환한다.

        경계는 `< timeout`(off-by-one 방지). 폴링/결과 URL은 request_id로 직접 구성한다.
        """
        status_url = f"{_FAL_QUEUE_BASE}/{self._app_id}/requests/{request_id}/status"
        elapsed = 0.0
        while elapsed < self._timeout:
            data, backoff_sec = self._status_once(status_url)
            elapsed += backoff_sec
            status = data.get("status")
            if status == "COMPLETED":
                return self._fetch_result(request_id)
            if status in ("IN_QUEUE", "IN_PROGRESS", None):
                self._sleep(self._interval)
                elapsed += self._interval
                continue
            # ERROR 등 종료 상태(미진행) — 명시적으로 실패한다(무한 폴링 방지).
            raise VideoRenderError(f"LipSync 작업 실패: status={status}")
        raise VideoTimeoutError(f"LipSync 폴링 타임아웃({self._timeout:.0f}s, 폴링 {self.poll_count}회)")

    def _status_once(self, url: str) -> tuple[dict, float]:
        """상태 1회 조회. 일시 오류(429/5xx)는 지수 backoff로 최대 3회 재시도.

        반환은 (응답 dict, 재시도 backoff 합계 초) — backoff는 호출부가 timeout에 누적.
        KlingClient._status_once와 동일 로직.
        """
        attempts = 0
        backoff_total = 0.0
        while True:
            self.poll_count += 1
            resp = _safe_send(
                lambda: self._client().get(url, headers=_fal_headers(self.settings)),
                "LipSync 상태 조회",
            )
            code = getattr(resp, "status_code", None)
            transient = isinstance(code, int) and (code == 429 or code >= 500)
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                wait = _RETRY_BACKOFF_SEC * (2 ** (attempts - 1))
                self._sleep(wait)
                backoff_total += wait
                continue
            return _json_or_raise(resp, "LipSync 상태 조회"), backoff_total

    def _fetch_result(self, request_id: str) -> tuple[str, bool]:
        """완료된 작업 결과에서 (검증된 영상 URL, has_audio)를 방어적으로 추출한다.

        fal 큐는 오류를 별도 ERROR 상태가 아니라 COMPLETED 응답 안 `error` 필드로
        담을 수 있으므로 결과 추출 전에 먼저 확인한다.
        """
        result_url = f"{_FAL_QUEUE_BASE}/{self._app_id}/requests/{request_id}"
        data = _send_json(
            lambda: self._client().get(result_url, headers=_fal_headers(self.settings)),
            "LipSync 결과 조회",
        )
        error = data.get("error")
        if error:
            err_type = data.get("error_type") or "unknown"
            log.debug("lipsync.result.error", error_type=err_type)
            raise VideoRenderError(f"LipSync 작업 실패: error_type={err_type}")
        uri = self._extract_video_url(data)
        if not uri:
            log.debug("lipsync.result.missing_url", keys=list(data.keys()))
            raise VideoRenderError(
                f"LipSync 결과에 영상 URL이 없습니다 (응답 키: {list(data.keys())})"
            )
        _validate_fal_video_url(uri)
        return uri, self._extract_has_audio(data)

    @staticmethod
    def _extract_video_url(data: dict) -> str | None:
        """결과에서 출력 영상 URL을 방어적으로 추출한다(snake/camelCase·중첩 모두 시도).

        문서상 출력은 `video.url`이지만(KlingClient와 동일), 스키마 변종(`output.url`,
        최상위 `url`)도 순서대로 시도한다. TODO(live): 라이브 응답 1건으로 실제 키 확정.
        """
        for key in ("video", "output"):
            node = data.get(key)
            if isinstance(node, dict):
                uri = node.get("url")
                if uri:
                    return str(uri)
        top = data.get("url")
        return str(top) if top else None

    @staticmethod
    def _extract_has_audio(data: dict) -> bool:
        """출력 MP4에 음성이 입혀졌는지 신호를 추출한다(없으면 True 가정).

        fal LipSync 응답에는 오디오 포함 여부 전용 필드가 없다. 모델 목적상 출력에는
        항상 음성이 입혀지므로 기본은 True(=백엔드가 mux 생략). 다만 응답에서 음성
        부재를 명시하는 신호(`has_audio=False`, `audio=None`, content_type이 영상이
        아닌 경우 등)가 잡히면 False로 폴백해 백엔드가 안전하게 기존 mux로 되돌리게
        한다. TODO(live): 라이브에서 출력 MP4를 ffprobe로 검사해 이 가정을 확정.
        """
        # 명시적 has_audio 플래그(snake/camelCase)가 있으면 그대로 따른다.
        for key in ("has_audio", "hasAudio"):
            if key in data and isinstance(data[key], bool):
                return data[key]
        # video 노드 안의 명시적 플래그도 확인.
        video = data.get("video")
        if isinstance(video, dict):
            for key in ("has_audio", "hasAudio"):
                if key in video and isinstance(video[key], bool):
                    return video[key]
            ctype = video.get("content_type") or video.get("contentType")
            # content_type이 영상이 아닌 값이면(예: image/*) 음성 보장 불가 → 폴백.
            if isinstance(ctype, str) and ctype and not ctype.startswith("video/"):
                return False
        # 신호 없음 → 항상 음성 포함 가정(모델 계약). 백엔드가 mux를 생략한다.
        return True

    def _download(self, uri: str) -> str:
        """검증된 영상 URL에서 바이트를 내려받아 media_dir에 저장하고 경로를 반환한다.

        KlingClient._download와 동일: CDN(fal.media)은 키 없이 받고(자격증명 미첨부),
        리다이렉트는 한 hop만 허용하며 그 Location도 호스트를 재검증한다(SSRF 체인 차단).
        """
        _validate_fal_video_url(uri)
        resp = _safe_send(
            lambda: self._client().get(uri, follow_redirects=False),
            "LipSync 영상 다운로드",
        )
        sc = getattr(resp, "status_code", None)
        if not isinstance(sc, int):
            raise VideoRenderError("LipSync 영상 다운로드 응답에 유효한 status_code가 없습니다")
        if 300 <= sc < 400:
            location = (getattr(resp, "headers", {}) or {}).get("location", "")
            if not location:
                raise VideoRenderError("LipSync 영상 다운로드: 리다이렉트 응답에 Location 헤더 없음")
            _validate_fal_video_url(location)
            resp = _safe_send(
                lambda: self._client().get(location, follow_redirects=False),
                "LipSync 영상 다운로드(리다이렉트)",
            )
            r_sc = getattr(resp, "status_code", None)
            if isinstance(r_sc, int) and 300 <= r_sc < 400:
                raise VideoRenderError("LipSync 다운로드: 허용 호스트 이후 추가 리다이렉트 금지")
        _raise_for_status(resp, "LipSync 영상 다운로드")
        content = getattr(resp, "content", None)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise VideoRenderError("LipSync 다운로드 응답에 영상 바이트가 없습니다")
        out_path = Path(self.settings.nutti_media_dir) / f"lipsync_{uuid4().hex[:12]}.mp4"
        _write_bytes(out_path, bytes(content), "LipSync 영상")
        log.info("lipsync.video.saved", path=str(out_path))
        return str(out_path)


class KlingVoiceoverBackend:
    """비트별 [Kling 무음 클립 + 한국어 TTS] → mux → 비트 클립 리스트를 만드는 백엔드.

    VideoStudio가 dry_run이 아닐 때 kling 백엔드로 호출한다. 클라이언트는 주입
    가능(테스트 fake)하며, 미주입 시 실 경로에서 지연 생성하고 finally에서 정확히
    1회 닫는다(연결 풀 누수 방지). mux/스티칭용 ffmpeg는 imageio-ffmpeg 번들을 쓴다.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        kling_client=None,
        tts_client=None,
        kling_lipsync_client=None,
        sleep=None,
    ):
        self.settings = settings
        self._kling_client = kling_client
        self._tts_client = tts_client
        # kling_lipsync=true 시 mux 대신 fal LipSync 후처리에 쓰는 클라이언트.
        # 미주입이면 실 경로에서 지연 생성하고 finally에서 1회 닫는다.
        self._kling_lipsync_client = kling_lipsync_client
        self._sleep = sleep

    def produce_beat_clips(self, frame_path: str, beats: list[str]) -> tuple[list[str], float]:
        """각 비트를 [무음 영상 + 내레이션] mux/립싱크 클립으로 만들어 (경로 리스트, 총길이초)를 반환한다.

        총길이초는 각 비트 클립의 실측 길이 합이다. mux는 `-shortest`로 출력을 두
        입력 중 짧은 쪽에 맞추므로 비트 클립 길이 ≈ min(무음 영상 길이=clip_dur,
        내레이션 길이=audio_sec)다. veo 경로의 8.0×N 가정과 달리 kling은 클립이
        5/10초이고 음성 길이로 잘리므로, 상위(VideoStudio)가 duration_sec을 실측에
        맞추도록 이 합계를 함께 반환한다(ffprobe 없이 audio_sec/clip_dur로 계산).

        kling_tts 분기:
          - 'gemini'(기본): GeminiTtsClient로 내레이션 합성.
          - 'elevenlabs': ElevenLabsTtsClient(아이 목소리)로 합성.
        kling_lipsync=true이면 mux 대신 KlingLipSyncClient로 립싱크 후처리를 수행한다.
        """
        builder = KlingPromptBuilder()
        kling = self._kling_client
        tts = self._tts_client
        lipsync = self._kling_lipsync_client
        owned_kling = owned_tts = owned_lipsync = None
        if kling is None:
            kling = owned_kling = KlingClient(self.settings, sleep=self._sleep)
        if tts is None:
            # kling_tts 설정에 따라 TTS 클라이언트를 분기한다.
            if self.settings.kling_tts == "elevenlabs":
                from nutti.integrations.tts_elevenlabs import ElevenLabsTtsClient

                tts = owned_tts = ElevenLabsTtsClient(self.settings, sleep=self._sleep)
            elif self.settings.kling_tts == "supertone":
                from nutti.integrations.tts_supertone import SupertoneTtsClient

                tts = owned_tts = SupertoneTtsClient(self.settings, sleep=self._sleep)
            else:
                tts = owned_tts = GeminiTtsClient(self.settings, sleep=self._sleep)
        if self.settings.kling_lipsync and lipsync is None:
            lipsync = owned_lipsync = KlingLipSyncClient(self.settings, sleep=self._sleep)
        clips: list[str] = []
        total_sec = 0.0
        try:
            for i, beat in enumerate(beats, start=1):
                voice_path, audio_sec = tts.synthesize(beat)
                silent_path: str | None = None
                # 립싱크 중간 산출물: None으로 초기화해 finally에서 안전하게 정리한다.
                lipsync_intermediate: str | None = None
                # kling.generate 이전에 예외가 발생해도 finally가 참조할 수 있도록 미리 초기화.
                clip_is_lipsync = False
                try:
                    clip_dur = _pick_clip_duration(audio_sec)
                    silent_path = kling.generate(frame_path, builder.build_beat(beat), clip_dur)
                    if self.settings.kling_lipsync and lipsync is not None:
                        # 립싱크 모드: mux 대신 fal LipSync로 마스코트 입을 음성에 맞춘다.
                        # KlingLipSyncClient.generate → (로컬 경로, has_audio).
                        # has_audio=True이면 출력에 음성이 포함된 것이므로 mux 불필요.
                        lipsync_path, has_audio = lipsync.generate(
                            silent_path,
                            voice_path,
                            video_sec=float(clip_dur),
                            audio_sec=float(audio_sec),
                        )
                        # 즉시 추적: _mux 실패 시에도 finally에서 정리할 수 있도록 기록.
                        lipsync_intermediate = lipsync_path
                        if has_audio:
                            # 립싱크 출력에 음성 포함 → WAV 별도 mux 불필요.
                            muxed = lipsync_path
                            clip_is_lipsync = True
                        else:
                            # 립싱크 결과에 음성 없음(예상치 못한 경우) → 안전 fallback: mux.
                            log.warning(
                                "lipsync.no_audio_fallback",
                                beat=i,
                                of=len(beats),
                            )
                            muxed = self._mux(lipsync_path, voice_path)
                            # mux 성공: lipsync 중간물을 여기서 제거할 수도 있지만
                            # finally가 lipsync_intermediate를 항상 정리하므로 생략한다.
                    else:
                        muxed = self._mux(silent_path, voice_path)
                finally:
                    # 비트 중간 산출물(무음 영상·내레이션 WAV·립싱크 중간물)은
                    # mux/립싱크 성공/실패와 무관하게 더 필요 없으므로 즉시 정리한다
                    # (수백 MB 누적·leak 방지).
                    # kling.generate가 실패하면 silent_path는 None이라 voice만 정리된다.
                    # has_audio=True 시 lipsync_intermediate == muxed가 되므로,
                    # muxed 자체를 삭제하지 않도록 clip_is_lipsync를 확인한다.
                    _unlink_quiet(voice_path)
                    _unlink_quiet(silent_path)
                    if not clip_is_lipsync:
                        # has_audio=False fallback 또는 예외 발생: 중간 립싱크 파일 정리.
                        _unlink_quiet(lipsync_intermediate)
                # mux -shortest 출력 길이 ≈ 두 입력 중 짧은 쪽(보통 내레이션=audio_sec).
                # 립싱크 has_audio=True 출력은 silent 영상과 동일한 clip_dur를 유지한다.
                if clip_is_lipsync:
                    total_sec += float(clip_dur)
                else:
                    total_sec += min(float(clip_dur), float(audio_sec))
                log.info("video.kling.clip.done", path=muxed, beat=i, of=len(beats))
                clips.append(muxed)
        except BaseException:
            # 비트 루프 중도 실패: 이미 완성된 beat_*.mp4(각 수백 MB)가 영구
            # leak되지 않도록 정리한 뒤 전파한다(현재 비트 중간물은 위 finally가 처리).
            for done in clips:
                _unlink_quiet(done)
            raise
        finally:
            if owned_kling is not None:
                _close_owned(owned_kling)
            if owned_tts is not None:
                _close_owned(owned_tts)
            if owned_lipsync is not None:
                _close_owned(owned_lipsync)
        return clips, total_sec

    def _mux(self, video_path: str, audio_path: str) -> str:
        """무음 영상에 내레이션 음성을 입혀(mux) 하나의 MP4로 만든다.

        `-shortest`로 출력 길이를 두 입력 중 짧은 쪽에 맞춘다 — 보이스오버는 클립
        길이 이하로 설계되므로(내레이션≤클립) 통상 음성 길이로 맞춰져 뒤쪽 무음
        구간이 생기지 않는다. 영상은 무재인코딩(copy), 음성만 AAC로 인코딩한다.
        실패(ffmpeg 비정상 종료·미설치)는 VideoRenderError로 변환(stderr 원문 미노출).
        """
        import subprocess

        import imageio_ffmpeg

        out_path = Path(self.settings.nutti_media_dir) / f"beat_{uuid4().hex[:12]}.mp4"
        cmd = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-i",
            video_path,
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            # ffmpeg가 비정상 종료해도 out_path에 truncated MP4가 남을 수 있다 —
            # _write_bytes의 OSError 핸들러처럼 부분 산출물을 정리한 뒤 전파한다
            # (수백 MB 영상이 media_dir에 누적되는 것을 방지). stderr 원문은 미노출.
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise VideoRenderError(f"내레이션 mux 실패: {type(exc).__name__}") from None
        log.info("video.kling.muxed", path=str(out_path))
        return str(out_path)
