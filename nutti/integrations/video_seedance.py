"""fal.ai Seedance 2.0 reference-to-video 백엔드 — TTS로 고정한 목소리에 립싱크한다.

Veo는 클립마다 음성을 새로 추첨하므로 목소리 고정이 **구조적으로** 불가능하다
(voice 파라미터도, 오디오 입력도 없다). Seedance 2.0 reference-to-video는 오디오를
레퍼런스(@Audio1)로 받아 그 오디오에 입을 맞추므로, TTS가 목소리를 고정하고 영상이
따라오는 구조가 된다(2026-07-29 파일럿: 강아지 얼굴 립싱크 작동 — PO 육안 확인).

흐름(비트별): `tts`로 대사 오디오 생성 → fal CDN 오디오 URL + 로컬 mp3 →
`generate`로 reference-to-video 제출(generate_audio=False → 무음 영상) → 폴링 →
다운로드. 무음 클립에 그 TTS 오디오를 붙이는 머지와 스티칭은 VideoStudio가 담당한다.

실측 근거(2026-07-30 openapi 스키마 + 2026-07-29 파일럿 산출물):
- `image_urls`/`audio_urls`는 배열이고 프롬프트에서 @Image1/@Audio1로 참조한다.
- `generate_audio=False`면 결과 mp4에 오디오 스트림이 아예 없다(ffmpeg 확인) →
  우리 TTS를 머지해야 소리가 난다. True로 두면 모델이 자체 음성을 만들어 우리
  목소리 고정이 무의미해진다.
- `duration`은 문자열 enum("4"~"15" | "auto"). 오디오보다 짧으면 대사가 잘리므로
  호출부가 오디오 길이를 올림해 넘긴다.
- TTS 결과 오디오 URL 호스트는 v3b.fal.media로 기존 `_FAL_SAFE_HOSTS`(fal.media)
  suffix 검증에 걸린다 → 재업로드 없이 그 URL을 그대로 Seedance에 넘긴다.

계약(video_veo_fal.FalVeoClient와 동일):
- 모든 오류는 VideoRenderError(폴링 초과는 VideoTimeoutError)로만 전파.
- redaction: 메시지에 URL·request id·응답 본문 금지(상태 코드·예외 타입명만).
- 주입 가능(http=/sleep=)으로 네트워크 없이 테스트.
- API 응답값(request id·미디어 URL)은 신뢰 불가 입력 → 형식·호스트 검증(SSRF 방어).
- dry_run 게이트는 상위 VideoStudio가 담당(이 클라이언트는 실 경로에서만 생성).

ponytail: 제출·폴링·다운로드가 FalVeoClient와 형태가 겹친다. 지금 _fal_common으로
추출하지 않는 이유는 두 백엔드 중 하나가 채택되면 다른 하나를 삭제하기 때문이다 —
Seedance가 채택되면 그때 veo_fal 경로를 지우고, 반대면 이 파일을 지운다.
"""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

from nutti.config import Settings
from nutti.integrations.video import (
    VideoRenderError,
    VideoTimeoutError,
    _json_or_raise,
    _raise_for_status,
    _safe_send,
    _send_json,
    _write_bytes,
)
from nutti.integrations._fal_common import (
    _FAL_QUEUE_BASE,
    _fal_headers,
    _guess_image_mime,
    _HttpClosingMixin,
    _MAX_TRANSIENT_RETRIES,
    _read_bytes,
    _RETRY_BACKOFF_SEC,
    _validate_fal_video_url,
    _validate_model_id,
    _validate_request_id,
)
from nutti.logging import get_logger

log = get_logger(__name__)

# Seedance duration 스키마가 허용하는 정수 초의 범위(문자열로 전송).
_MIN_DURATION_SEC = 4
_MAX_DURATION_SEC = 15


class FalSeedanceClient(_HttpClosingMixin):
    """fal.ai Seedance 2.0 립싱크 클라이언트(TTS 생성 + 오디오 기반 영상 생성).

    폴링 간격·타임아웃은 veo_fal 설정값(NUTTI_VEO_FAL_POLL_INTERVAL_SEC /
    NUTTI_VEO_FAL_TIMEOUT_SEC)을 공유한다 — 같은 fal 큐를 같은 방식으로 기다리므로
    백엔드마다 같은 값을 두 벌 두지 않는다.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep if sleep is not None else time.sleep
        # 설정값을 URL에 삽입하기 전에 형식 검증(주입 표면 제한).
        self._model = _validate_model_id(
            settings.seedance_model, env_name="NUTTI_SEEDANCE_MODEL"
        )
        self._tts_model = _validate_model_id(
            settings.seedance_tts_model, env_name="NUTTI_SEEDANCE_TTS_MODEL"
        )
        self._interval = float(settings.veo_fal_poll_interval_sec)
        if self._interval <= 0:
            raise ValueError(
                f"veo_fal_poll_interval_sec는 0보다 커야 합니다(현재 {self._interval})"
            )
        self._timeout = float(settings.veo_fal_timeout_sec)
        if self._timeout <= 0:
            raise ValueError(f"veo_fal_timeout_sec는 0보다 커야 합니다(현재 {self._timeout})")
        # 진단용: 폴링 HTTP 시도 횟수(타임아웃 메시지에 포함).
        self.poll_count = 0

    def _client(self):
        """httpx 클라이언트를 지연 확보(주입 우선). dry_run에서는 호출되지 않는다."""
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    @staticmethod
    def _app_id(model: str) -> str:
        """fal 큐 status/result 조회에 쓰는 앱 ID(모델 경로 앞 2세그먼트).

        전체 모델 경로로 GET하면 405가 난다(FalVeoClient와 동일한 fal 큐 동작).
        "bytedance/seedance-2.0/reference-to-video" → "bytedance/seedance-2.0"
        """
        segs = model.split("/")
        return "/".join(segs[:2]) if len(segs) >= 2 else model

    # ---------------------------------------------------------------- TTS

    def tts(self, text: str) -> tuple[str, str]:
        """대사 한 줄을 목소리로 만들고 (fal CDN 오디오 URL, 로컬 mp3 경로)를 반환한다.

        URL을 함께 돌려주는 이유: Seedance에는 이 URL을 그대로 넘겨 재업로드를 없애고,
        로컬 파일은 길이 측정(영상 duration 결정)과 무음 클립 머지에 쓴다.

        `voice_instruction`·`pitch`는 값이 있을 때만 페이로드에 실는다 — 기본
        seed-speech v2 스키마의 선택 필드이며, TTS 모델을 다른 것으로 교체하면
        이 필드들을 받지 않을 수 있다(그 경우 설정을 비워 둔다).
        """
        line = (text or "").strip()
        if not line:
            raise VideoRenderError("Seedance TTS: 빈 대사는 합성할 수 없습니다")
        body: dict = {
            "text": line,
            "voice": self.settings.seedance_tts_voice,
            "output_format": "mp3",
        }
        instruction = (self.settings.seedance_tts_voice_instruction or "").strip()
        if instruction:
            body["voice_instruction"] = instruction
        pitch = int(self.settings.seedance_tts_pitch)
        if pitch:
            body["pitch"] = pitch
        data = self._run(self._tts_model, body, "Seedance TTS")
        audio = data.get("audio")
        uri = audio.get("url") if isinstance(audio, dict) else None
        if not uri:
            # redaction: 예외 메시지에 응답 본문(키 목록 포함) 금지 — 진단은 log.debug로만.
            log.debug("seedance.tts.missing_url", keys=list(data.keys()))
            raise VideoRenderError("Seedance TTS 결과에 오디오 URL이 없습니다")
        uri = str(uri)
        _validate_fal_video_url(uri)
        return uri, self._download(uri, "seedance_tts", ".mp3", "Seedance TTS 오디오")

    # -------------------------------------------------------------- 영상

    def generate(
        self, frame_path: str, prompt: str, *, audio_url: str, duration_sec: float
    ) -> str:
        """시작 프레임 + 프롬프트 + 오디오로 무음 립싱크 클립을 만들고 저장 경로를 반환한다.

        `duration_sec`(TTS 실측 길이)는 올림해서 스키마 범위(4~15초)로 자른 뒤 보낸다 —
        오디오보다 짧으면 대사 끝이 잘리므로 올림이 안전한 방향이다. 남는 뒷부분은
        머지 시 오디오 길이로 잘려 나간다.
        """
        import math

        _validate_fal_video_url(audio_url)
        duration = max(
            _MIN_DURATION_SEC, min(_MAX_DURATION_SEC, int(math.ceil(duration_sec or 0)))
        )
        body = {
            "prompt": prompt,
            "image_urls": [self._encode_data_uri(frame_path, "Seedance 시작 프레임")],
            "audio_urls": [audio_url],
            "duration": str(duration),
            "resolution": self.settings.seedance_resolution,
            "aspect_ratio": "9:16",
            # 우리 TTS로 목소리를 고정하는 것이 이 백엔드의 존재 이유다 — 모델이 자체
            # 음성을 만들면 그 고정이 무의미해진다. 결과는 무음이므로 호출부가 머지한다.
            "generate_audio": False,
        }
        data = self._run(self._model, body, "Seedance 영상")
        video = data.get("video")
        uri = video.get("url") if isinstance(video, dict) else None
        if not uri:
            log.debug("seedance.video.missing_url", keys=list(data.keys()))
            raise VideoRenderError("Seedance 결과에 영상 URL이 없습니다")
        uri = str(uri)
        _validate_fal_video_url(uri)
        return self._download(uri, "seedance", ".mp4", "Seedance 영상")

    # ------------------------------------------------------- 큐 공통 흐름

    def _run(self, model: str, body: dict, what: str) -> dict:
        """제출 → 완료 폴링 → 결과 JSON. TTS·영상이 같은 fal 큐 규약을 공유한다."""
        app_id = self._app_id(model)
        request_id = self._submit(model, body, what)
        return self._poll(app_id, request_id, what)

    def _submit(self, model: str, body: dict, what: str) -> str:
        """작업을 제출하고 검증된 request_id를 반환한다(429/5xx는 backoff 재시도)."""
        data = _send_json(
            lambda: self._client().post(
                f"{_FAL_QUEUE_BASE}/{model}", headers=_fal_headers(self.settings), json=body
            ),
            f"{what} 작업 제출",
            sleep=self._sleep,
            max_transient_retries=_MAX_TRANSIENT_RETRIES,
        )
        request_id = data.get("request_id")
        if not request_id:
            log.debug("seedance.submit.missing_request_id", keys=list(data.keys()))
            raise VideoRenderError(f"{what} 응답에 request_id가 없습니다")
        return _validate_request_id(str(request_id))

    def _poll(self, app_id: str, request_id: str, what: str) -> dict:
        """상태를 COMPLETED까지 폴링하고 결과 JSON을 반환한다.

        경계는 `< timeout`(off-by-one 방지). ERROR 등 종료 상태는 즉시 실패시킨다.
        """
        status_url = f"{_FAL_QUEUE_BASE}/{app_id}/requests/{request_id}/status"
        elapsed = 0.0
        while elapsed < self._timeout:
            data, backoff_sec = self._status_once(status_url, what)
            elapsed += backoff_sec
            status = data.get("status")
            if status == "COMPLETED":
                return self._fetch_result(app_id, request_id, what)
            if status in ("IN_QUEUE", "IN_PROGRESS", None):
                self._sleep(self._interval)
                elapsed += self._interval
                continue
            raise VideoRenderError(f"{what} 작업 실패: status={status}")
        raise VideoTimeoutError(
            f"{what} 폴링 타임아웃({self._timeout:.0f}s, 폴링 {self.poll_count}회)"
        )

    def _status_once(self, url: str, what: str) -> tuple[dict, float]:
        """상태 1회 조회. 일시 오류(429/5xx)는 지수 backoff로 최대 3회 재시도.

        반환은 (응답 dict, 재시도 backoff 합계 초) — backoff는 호출부가 timeout에 누적.
        """
        attempts = 0
        backoff_total = 0.0
        while True:
            self.poll_count += 1
            resp = _safe_send(
                lambda: self._client().get(url, headers=_fal_headers(self.settings)),
                f"{what} 상태 조회",
            )
            code = getattr(resp, "status_code", None)
            transient = isinstance(code, int) and (code == 429 or code >= 500)
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                wait = _RETRY_BACKOFF_SEC * (2 ** (attempts - 1))
                self._sleep(wait)
                backoff_total += wait
                continue
            return _json_or_raise(resp, f"{what} 상태 조회"), backoff_total

    def _fetch_result(self, app_id: str, request_id: str, what: str) -> dict:
        """완료된 작업의 결과 JSON을 가져온다(이미 과금된 결과를 조회 실패로 잃지 않게 재시도).

        간헐 400/422까지 재시도한다(retry_4xx) — 2026-07-30 실측: 같은 프레임·오디오·
        프롬프트로 낸 요청의 결과 조회가 한 번은 422였고 직후 두 번은 200이었다.
        """
        return _send_json(
            lambda: self._client().get(
                f"{_FAL_QUEUE_BASE}/{app_id}/requests/{request_id}",
                headers=_fal_headers(self.settings),
            ),
            f"{what} 결과 조회",
            sleep=self._sleep,
            max_transient_retries=_MAX_TRANSIENT_RETRIES,
            retry_4xx=True,
        )

    # ------------------------------------------------------------- 유틸

    def _encode_data_uri(self, frame_path: str, label: str) -> str:
        """프레임 파일을 base64 data URI로 인코딩한다(fal 이미지 입력용).

        FalVeoClient가 같은 방식으로 Veo에 프레임을 넣고 있어 fal가 data URI를 받는 것은
        프로덕션에서 검증돼 있다 — 별도 스토리지 업로드 단계를 두지 않는 이유다.
        """
        import base64

        frame_bytes = _read_bytes(frame_path, label)
        mime = _guess_image_mime(frame_path)
        return f"data:{mime};base64,{base64.b64encode(frame_bytes).decode('ascii')}"

    def _download(self, uri: str, prefix: str, suffix: str, what: str) -> str:
        """검증된 fal CDN URL에서 바이트를 내려받아 media_dir에 저장하고 경로를 반환한다.

        CDN은 키 없이 내려받는다(자격증명 헤더 미첨부). 리다이렉트는 한 hop만 허용하고
        그 Location도 호스트를 재검증한다(SSRF 체인 차단) — FalVeoClient와 동일 정책.
        """
        _validate_fal_video_url(uri)
        resp = _safe_send(lambda: self._client().get(uri, follow_redirects=False), f"{what} 다운로드")
        sc = getattr(resp, "status_code", None)
        if not isinstance(sc, int):
            raise VideoRenderError(f"{what} 다운로드 응답에 유효한 status_code가 없습니다")
        if 300 <= sc < 400:
            location = (getattr(resp, "headers", {}) or {}).get("location", "")
            if not location:
                raise VideoRenderError(f"{what} 다운로드: 리다이렉트 응답에 Location 헤더 없음")
            _validate_fal_video_url(location)
            resp = _safe_send(
                lambda: self._client().get(location, follow_redirects=False),
                f"{what} 다운로드(리다이렉트)",
            )
            r_sc = getattr(resp, "status_code", None)
            if isinstance(r_sc, int) and 300 <= r_sc < 400:
                raise VideoRenderError(f"{what} 다운로드: 허용 호스트 이후 추가 리다이렉트 금지")
        _raise_for_status(resp, f"{what} 다운로드")
        content = getattr(resp, "content", None)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise VideoRenderError(f"{what} 다운로드 응답에 바이트가 없습니다")
        out_path = Path(self.settings.nutti_media_dir) / f"{prefix}_{uuid4().hex[:12]}{suffix}"
        _write_bytes(out_path, bytes(content), what)
        log.info("seedance.media.saved", path=str(out_path))
        return str(out_path)
