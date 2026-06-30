"""fal.ai Veo 3.1 image-to-video 백엔드.

Gemini API Veo와 동일한 모델(Veo 3.1)을 fal.ai 종량제로 호스팅한다.
Gemini API의 일일 쿼터 벽을 우회하면서 Veo 품질·네이티브 한국어 음성·마스코트 일관성을 유지한다
(같은 Veo 모델, fal.ai 종량제 호스팅). Lite 화질로 싸게 먼저 검증하고, PO 승인 후
NUTTI_VEO_FAL_MODEL만 Fast로 교체하면 바로 승격된다.

흐름(비트별): 비트마다 같은 시작 프레임에서 VeoPromptBuilder.build_beat 프롬프트로
  8초 클립을 생성 → fal 큐 REST: 제출(_submit) → 폴링(_poll, COMPLETED까지) →
  다운로드(_download) → VideoStudio._stitch로 비트 클립 합산.
  (Veo extend는 fal에 미노출 — 비트마다 독립 생성·스티칭.)

fal 큐 공통 헬퍼(_fal_common.py): _FAL_QUEUE_BASE, _fal_headers, _validate_request_id,
_validate_model_id, _validate_fal_video_url, _HttpClosingMixin, _guess_image_mime, _read_bytes
를 재사용한다(중복 구현 금지).

계약(video.py와 동일):
- 모든 오류는 VideoRenderError(폴링 초과는 VideoTimeoutError)로만 전파.
- redaction: 메시지에 URL·request id·응답 본문 금지(상태 코드·예외 타입명만).
- 주입 가능(http=/sleep=)으로 네트워크 없이 테스트.
- _HttpClosingMixin으로 연결 풀 정리.
- API 응답값(request id·영상 URL)은 신뢰 불가 입력 → 형식·호스트 검증(SSRF 방어).
- dry_run 게이트는 상위 VideoStudio가 담당(여기 클라이언트는 실 경로에서만 생성).

라이브 스키마 참조(2026-06-16 WebFetch 확인, fal.ai openapi):
  POST https://queue.fal.run/fal-ai/veo3.1/lite/image-to-video
  입력 필드: prompt, image_url, generate_audio(bool), aspect_ratio("9:16" 등),
              resolution("720p"|"1080p"), duration("4s"|"6s"|"8s")
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


class FalVeoClient(_HttpClosingMixin):
    """fal.ai Veo 3.1 image-to-video 클라이언트(제출 → 폴링 → 즉시 다운로드).

    KlingClient와 동일한 fal 큐 REST 패턴을 따른다:
    ① `POST {base}/{model}`로 작업 제출(시작 프레임 base64 data URI + 프롬프트) →
    ② request_id를 받아 `GET {base}/{model}/requests/{id}/status`를 폴링 →
    ③ COMPLETED면 `GET {base}/{model}/requests/{id}`로 결과를 받아
    영상 URL을 검증·다운로드해 media_dir에 저장.

    KlingClient와의 차이점:
    - 제출 페이로드: image_url(data URI), prompt, generate_audio, aspect_ratio, resolution, duration.
    - 모델이 단일 레벨(앱 ID = 전체 경로 "fal-ai/veo3.1")이므로 app_id를 앞 2세그먼트로
      잘라 status/result 조회에 쓴다(Kling의 405 문제와 동일한 fal 큐 동작).
    - extend 미사용 — fal Veo는 extend 엔드포인트를 미노출.

    오류 계약: HTTP·전송·JSON·쓰기 실패 → VideoRenderError, 폴링 초과 → VideoTimeoutError.
    일시 오류(429/5xx)는 backoff로 최대 3회 재시도. 자격증명(Authorization)은
    queue.fal.run 요청에만, CDN 다운로드(fal.media)에는 붙이지 않는다.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep if sleep is not None else time.sleep
        # 끝프레임 고정 모드(2026-06-29 PO): True면 first-last-frame-to-video 모델을 써
        # 각 비트 클립의 시작·끝 프레임을 동일 마스코트 프레임으로 고정한다(비트 경계 끊김
        # 근본 완화). 입력 필드가 다르므로(_submit 참조) 모드 플래그를 보관한다.
        self._endframe_lock = bool(settings.veo_fal_endframe_lock)
        # 설정값을 URL에 삽입하기 전에 형식 검증(주입 표면 제한). 모드에 맞는 모델을 고른다.
        if self._endframe_lock:
            self._model = _validate_model_id(
                settings.veo_fal_flf_model, env_name="NUTTI_VEO_FAL_FLF_MODEL"
            )
        else:
            self._model = _validate_model_id(
                settings.veo_fal_model, env_name="NUTTI_VEO_FAL_MODEL"
            )
        # fal 큐 status/result 조회는 앱 ID(앞 2세그먼트)만 사용한다.
        # "fal-ai/veo3.1/lite/image-to-video" → app_id = "fal-ai/veo3.1"
        # (KlingClient의 405 실측 사례와 동일 — 전체 모델 경로 GET은 405 발생).
        _segs = self._model.split("/")
        self._app_id = "/".join(_segs[:2]) if len(_segs) >= 2 else self._model
        # 폴링 간격·타임아웃 검증(0 이하면 무한 루프·즉시 타임아웃 위험).
        self._interval = float(settings.veo_fal_poll_interval_sec)
        if self._interval <= 0:
            raise ValueError(
                f"veo_fal_poll_interval_sec는 0보다 커야 합니다(현재 {self._interval})"
            )
        self._timeout = float(settings.veo_fal_timeout_sec)
        if self._timeout <= 0:
            raise ValueError(
                f"veo_fal_timeout_sec는 0보다 커야 합니다(현재 {self._timeout})"
            )
        # 진단용: 폴링 HTTP 시도 횟수(타임아웃 메시지에 포함).
        self.poll_count = 0

    def _client(self):
        """httpx 클라이언트를 지연 확보(주입 우선). dry_run에서는 호출되지 않는다."""
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    def generate(
        self,
        frame_path: str,
        prompt: str,
        *,
        last_frame_path: str | None = None,
        seed: int | None = None,
    ) -> str:
        """시작 프레임 + 프롬프트로 8초 클립을 생성하고 로컬 저장 경로를 반환한다.

        끝프레임 고정 모드(endframe_lock)에서는 `last_frame_path`(끝 프레임)도 함께
        제출한다 — 미지정 시 시작 프레임과 동일 프레임으로 고정해 경계를 매끄럽게 한다.
        `seed`를 주면 제출 페이로드에 실어 영상 내 비트 간 음색/비주얼 편차를 줄인다
        (호출부가 한 영상의 모든 비트에 같은 seed를 넘긴다).
        """
        request_id = self._submit(frame_path, prompt, last_frame_path=last_frame_path, seed=seed)
        video_url = self._poll(request_id)
        return self._download(video_url)

    def _encode_data_uri(self, frame_path: str, label: str) -> str:
        """프레임 파일을 base64 data URI로 인코딩한다(fal 입력 image/frame URL용)."""
        import base64

        frame_bytes = _read_bytes(frame_path, label)
        mime = _guess_image_mime(frame_path)
        return f"data:{mime};base64,{base64.b64encode(frame_bytes).decode('ascii')}"

    def _submit(
        self,
        frame_path: str,
        prompt: str,
        *,
        last_frame_path: str | None = None,
        seed: int | None = None,
    ) -> str:
        """작업을 제출하고 검증된 request_id를 반환한다.

        모드별 입력 스키마:
        - 기본(image-to-video, 2026-06-16 openapi): prompt, image_url, generate_audio,
          aspect_ratio, resolution, duration.
        - 끝프레임 고정(first-last-frame-to-video, 2026-06-29 확인): image_url 대신
          first_frame_url + last_frame_url. last_frame_path 미지정 시 시작 프레임과 동일
          프레임으로 고정 — 모든 비트가 같은 포즈로 시작·종료해 비트 경계가 매끄럽다.
        시작/끝 프레임은 KlingClient._submit과 동일하게 base64 data URI로 보낸다.
        """
        data_uri = self._encode_data_uri(frame_path, "Veo(fal) 시작 프레임")
        body = {
            "prompt": prompt,
            "generate_audio": True,
            "aspect_ratio": "9:16",
            "resolution": self.settings.veo_fal_resolution,
            "duration": "8s",
        }
        if self._endframe_lock:
            # 끝 프레임 미지정이면 시작 프레임과 동일 프레임으로 고정(경계 매끄러움 핵심).
            last_uri = (
                self._encode_data_uri(last_frame_path, "Veo(fal) 끝 프레임")
                if last_frame_path
                else data_uri
            )
            body["first_frame_url"] = data_uri
            body["last_frame_url"] = last_uri
        else:
            body["image_url"] = data_uri
        # 화면 자막(깨진 한글 텍스트) 억제 — fal Veo 3.1이 지원하는 negative_prompt로
        # 보낸다(2026-06-18 스키마 확인). 설정이 비어 있으면 필드를 생략한다.
        negative_prompt = (self.settings.veo_fal_negative_prompt or "").strip()
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        # 음색 일관성 보강 seed(영상 내 모든 비트 동일값). None이면 필드를 생략해 Veo가
        # 매번 랜덤 생성한다(기존 동작). veo3.1 스키마의 optional seed 필드.
        if seed is not None:
            body["seed"] = int(seed)
        url = f"{_FAL_QUEUE_BASE}/{self._model}"
        # 일시 오류(429/5xx)는 backoff 재시도(폴링·결과조회와 동일). Veo 생성은 분당
        # 한도가 낮은 시점에 제출 429를 맞으면 전체 파이프라인이 죽을 수 있으므로 재시도한다.
        # 400은 재시도하지 않는다(Veo fal 제출에서 간헐 400 실측 없음).
        data = _send_json(
            lambda: self._client().post(url, headers=_fal_headers(self.settings), json=body),
            "Veo(fal) 작업 제출",
            sleep=self._sleep,
            max_transient_retries=_MAX_TRANSIENT_RETRIES,
        )
        request_id = data.get("request_id")
        if not request_id:
            # redaction: 예외 메시지에 응답 본문(키 목록 포함) 금지 — 진단은 log.debug로만.
            log.debug("veo_fal.submit.missing_request_id", keys=list(data.keys()))
            raise VideoRenderError("Veo(fal) 응답에 request_id가 없습니다")
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
            # ERROR 등 종료 상태 — 명시적으로 실패한다(무한 폴링 방지).
            raise VideoRenderError(f"Veo(fal) 작업 실패: status={status}")
        raise VideoTimeoutError(
            f"Veo(fal) 폴링 타임아웃({self._timeout:.0f}s, 폴링 {self.poll_count}회)"
        )

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
                "Veo(fal) 상태 조회",
            )
            code = getattr(resp, "status_code", None)
            transient = isinstance(code, int) and (code == 429 or code >= 500)
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                wait = _RETRY_BACKOFF_SEC * (2 ** (attempts - 1))
                self._sleep(wait)
                backoff_total += wait
                continue
            return _json_or_raise(resp, "Veo(fal) 상태 조회"), backoff_total

    def _fetch_result_url(self, request_id: str) -> str:
        """완료된 작업의 결과에서 검증된 영상 URL을 방어적으로 추출·반환한다."""
        result_url = f"{_FAL_QUEUE_BASE}/{self._app_id}/requests/{request_id}"
        # 결과 조회도 일시 오류(429/5xx) 재시도 — Veo 생성이 끝난 뒤(과금 완료) 결과
        # 조회 한 번의 429로 전체 비용이 날아가는 손실을 막는다(폴링과 동일 backoff).
        data = _send_json(
            lambda: self._client().get(result_url, headers=_fal_headers(self.settings)),
            "Veo(fal) 결과 조회",
            sleep=self._sleep,
            max_transient_retries=_MAX_TRANSIENT_RETRIES,
        )
        # fal Veo 결과 스키마: {"video": {"url": "..."}} — KlingClient와 동일 구조.
        video = data.get("video")
        uri = video.get("url") if isinstance(video, dict) else None
        if not uri:
            # redaction: 예외 메시지에 응답 본문(키 목록 포함) 금지 — 진단은 log.debug로만.
            log.debug("veo_fal.result.missing_url", keys=list(data.keys()))
            raise VideoRenderError("Veo(fal) 결과에 영상 URL이 없습니다")
        uri = str(uri)
        _validate_fal_video_url(uri)
        return uri

    def _download(self, uri: str) -> str:
        """검증된 영상 URL에서 바이트를 내려받아 media_dir에 저장하고 경로를 반환한다.

        CDN(fal.media)은 키 없이 내려받는다(자격증명 헤더 미첨부).
        KlingClient._download와 동일: 리다이렉트는 한 hop만 허용하고
        그 Location도 호스트를 재검증한다(SSRF 체인 차단).
        """
        _validate_fal_video_url(uri)
        resp = _safe_send(
            lambda: self._client().get(uri, follow_redirects=False),
            "Veo(fal) 영상 다운로드",
        )
        sc = getattr(resp, "status_code", None)
        if not isinstance(sc, int):
            raise VideoRenderError("Veo(fal) 영상 다운로드 응답에 유효한 status_code가 없습니다")
        if 300 <= sc < 400:
            location = (getattr(resp, "headers", {}) or {}).get("location", "")
            if not location:
                raise VideoRenderError("Veo(fal) 영상 다운로드: 리다이렉트 응답에 Location 헤더 없음")
            _validate_fal_video_url(location)
            resp = _safe_send(
                lambda: self._client().get(location, follow_redirects=False),
                "Veo(fal) 영상 다운로드(리다이렉트)",
            )
            r_sc = getattr(resp, "status_code", None)
            if isinstance(r_sc, int) and 300 <= r_sc < 400:
                raise VideoRenderError("Veo(fal) 다운로드: 허용 호스트 이후 추가 리다이렉트 금지")
        _raise_for_status(resp, "Veo(fal) 영상 다운로드")
        content = getattr(resp, "content", None)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise VideoRenderError("Veo(fal) 다운로드 응답에 영상 바이트가 없습니다")
        out_path = Path(self.settings.nutti_media_dir) / f"veo_fal_{uuid4().hex[:12]}.mp4"
        _write_bytes(out_path, bytes(content), "Veo(fal) 영상")
        log.info("veo_fal.video.saved", path=str(out_path))
        return str(out_path)
