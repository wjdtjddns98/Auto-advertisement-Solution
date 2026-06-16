"""fal.ai FLUX.1 Kontext [pro] 이미지 편집으로 영상 시작 프레임을 생성하는 클라이언트.

FLUX.1 Kontext는 레퍼런스 편집 모델: assets/mascot.png를 image_url로 넣고
의상·장소 지시 프롬프트를 주면 마스코트 강아지를 유지한 채 편집한다.
레퍼런스 이미지(마스코트)가 없으면 캐릭터 일관성이 보장되지 않으므로 오류로 처리한다.

fal 큐 REST 흐름:
  ① POST {base}/{model} — 레퍼런스 이미지(base64 data URI) + 프롬프트 제출
  ② GET {base}/{app_id}/requests/{id}/status — COMPLETED까지 폴링
  ③ GET {base}/{app_id}/requests/{id} — 결과 {"images": [{"url": ...}]} 추출
  ④ GET <이미지 URL> — CDN 다운로드(키 미첨부), media_dir에 원자적 저장

보안 계약(PR #65 교훈 선반영):
- _submit / _fetch_result_url: _send_json에 sleep + max_transient_retries 적용
  → 제출 직전/결과 직후 429 한 번에 파이프라인 전체가 죽지 않는다.
- redaction: 예외 메시지에 응답 키 목록 금지 — log.debug로만 기록.
- SSRF 방어: 다운로드 URL host를 _FAL_SAFE_HOSTS로 검증(v3.fal.media 포함).
- FAL_KEY는 queue.fal.run / rest.alpha.fal.ai 요청에만, CDN/스토리지 PUT에는 미첨부.

레퍼런스 업로드(2026-06 실측 follow-up 반영):
  data URI 대신 fal-storage에 먼저 업로드해 https file_url로 넘긴다. 대용량 base64
  data URI를 image_url로 주면 Kontext가 레퍼런스를 못 읽고 기본 해상도(예: 1024x768)
  검정 placeholder를 돌려주는 정황이 라이브에서 관측됨(검정 프레임 주범). 흐름:
  ① POST {rest}/storage/upload/initiate?storage_type=fal-cdn-v3 (Authorization: Key)
     → {"file_url", "upload_url"}
  ② PUT {upload_url} 원본 바이트(Content-Type만, 자격증명 미첨부 — presigned)
  ③ file_url(*.fal.media)을 _submit의 image_url로 사용.

모든 오류는 VideoRenderError(폴링 초과는 VideoTimeoutError)로만 전파한다.
주입 가능(http=/sleep=)으로 네트워크 없이 테스트.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from nutti.config import Settings
from nutti.integrations.video import (
    VideoRenderError,
    VideoTimeoutError,
    _HttpClosingMixin,
    _raise_for_status,
    _read_bytes,
    _safe_send,
    _send_json,
    _write_bytes,
)
from nutti.integrations._fal_common import (
    _FAL_QUEUE_BASE,
    _FAL_SAFE_HOSTS,
    _MAX_TRANSIENT_RETRIES,
    _RETRY_BACKOFF_SEC,
    _fal_headers,
    _guess_image_mime,
    _validate_model_id,
    _validate_request_id,
)
from nutti.logging import get_logger

log = get_logger(__name__)

# fal-storage REST 베이스(레퍼런스 업로드 initiate 전용). 자격증명(Authorization: Key)은
# 이 호스트와 queue.fal.run에만 붙인다 — presigned upload_url(PUT)·CDN 다운로드엔 미첨부.
_FAL_REST_BASE = "https://rest.alpha.fal.ai"


class FalKontextClient(_HttpClosingMixin):
    """fal.ai FLUX.1 Kontext [pro]로 영상 시작 프레임을 생성하는 클라이언트.

    NanoBananaClient와 동일한 공개 시그니처를 유지한다:
      generate_frame(scene_prompt, *, reference_image_path=None) -> str

    레퍼런스 이미지는 마스코트 일관성의 핵심이므로 None이면 VideoRenderError로
    즉시 실패한다(NUTTI_MASCOT_IMAGE 미설정 시 진단 안내 포함).

    fal 큐 패턴은 FalVeoClient를 그대로 본뜬다:
    ① _submit → ② _poll(_status_once 루프) → ③ _fetch_result_url → ④ _download.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep if sleep is not None else time.sleep
        # 설정값을 URL에 삽입하기 전에 형식 검증(주입 표면 제한).
        self._model = _validate_model_id(
            settings.kontext_model, env_name="NUTTI_KONTEXT_MODEL"
        )
        # fal 큐 status/result 조회는 앱 ID(앞 2세그먼트)만 사용한다.
        # "fal-ai/flux-pro/kontext" → app_id = "fal-ai/flux-pro"
        # (FalVeoClient와 동일한 fal 큐 GET 405 방어 패턴).
        _segs = self._model.split("/")
        self._app_id = "/".join(_segs[:2]) if len(_segs) >= 2 else self._model
        # 폴링 간격·타임아웃 검증. 이미지 생성은 영상보다 빠르므로 기본값이 작다.
        self._interval = float(settings.kontext_poll_interval_sec)
        if self._interval <= 0:
            raise ValueError(
                f"kontext_poll_interval_sec는 0보다 커야 합니다(현재 {self._interval})"
            )
        self._timeout = float(settings.kontext_timeout_sec)
        if self._timeout <= 0:
            raise ValueError(
                f"kontext_timeout_sec는 0보다 커야 합니다(현재 {self._timeout})"
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

    # 정상 프레임은 수백 KB~수 MB. 검정/빈/실패 프레임은 ~10KB로 비정상적으로 작다(실측:
    # 검정 10KB 프레임이 영상에 무에서 다른 캐릭터를 만들게 했다). 이 미만이면 퇴화로 보고
    # 재시도한다 — 디코딩 라이브러리 없이 가능한 경량 검정/실패 휴리스틱.
    _MIN_FRAME_BYTES = 51_200
    _MAX_FRAME_RETRIES = 2

    def generate_frame(
        self, scene_prompt: str, *, reference_image_path: str | None = None
    ) -> str:
        """시작 프레임 이미지를 생성해 media_dir에 저장하고 로컬 경로를 반환한다.

        NanoBananaClient.generate_frame과 동일한 시그니처를 유지한다 —
        VideoStudio._generate_frame이 self._nano_client를 통해 호출하므로.

        reference_image_path가 None이면 마스코트 일관성이 보장되지 않으므로
        VideoRenderError로 즉시 실패한다(NUTTI_MASCOT_IMAGE 미설정 안내).

        Kontext가 간헐적으로 검정/빈 프레임(파일이 비정상적으로 작음)을 돌려주면 영상
        단계에서 캐릭터가 무에서 제각각 생성되므로, 그런 프레임은 거부하고 재시도한다.
        계속 비정상이면 영상(과금) 단계로 넘기지 않고 명확히 실패한다.
        """
        if reference_image_path is None:
            raise VideoRenderError(
                "Kontext는 레퍼런스 이미지가 필요합니다 — "
                "NUTTI_MASCOT_IMAGE 설정 필요"
            )
        for attempt in range(self._MAX_FRAME_RETRIES + 1):
            request_id = self._submit(scene_prompt, reference_image_path)
            image_url = self._poll(request_id)
            path = self._download(image_url)
            reason = self._reject_reason(path)
            if reason is None:
                return path
            # 검정/퇴화 프레임(작거나 세로 비율 위반) — 정리하고 재시도.
            log.warning("kontext.frame.degenerate", reason=reason, attempt=attempt)
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                # 정리 실패(Windows 핸들 등)는 무해 — 핵심은 이 프레임을 수락하지 않는 것.
                pass
        raise VideoRenderError(
            "Kontext 프레임이 반복적으로 비정상(검정/빈/가로 이미지로 의심)입니다 — "
            "fal 응답·레퍼런스·프롬프트를 확인하세요"
        )

    def _reject_reason(self, path: str) -> str | None:
        """수락 가능한 프레임이면 None, 퇴화 프레임이면 거부 사유 문자열을 반환한다.

        두 단계 휴리스틱(외부 디코딩 라이브러리 없이):
        ① 바이트 크기 < _MIN_FRAME_BYTES → 검정/빈 프레임 의심("too_small").
        ② PNG이고 IHDR 해상도가 파싱되면 세로(9:16)가 아니면 거부("not_portrait") —
           라이브에서 레퍼런스 로딩 실패 시 1024x768(가로) 검정 placeholder가 관측됨.
           해상도가 파싱 안 되는 형식(jpg 등)은 ①만으로 판정(회귀 방지).
        """
        try:
            data = Path(path).read_bytes()
        except OSError:
            return "unreadable"
        if len(data) < self._MIN_FRAME_BYTES:
            return "too_small"
        dims = _png_dimensions(data)
        if dims is not None:
            width, height = dims
            # Kontext는 aspect_ratio=9:16(세로)로 제출하므로 정상 결과는 height > width.
            # 가로/정사각이면 레퍼런스 미적용 placeholder로 보고 거부한다.
            if height <= width:
                return "not_portrait"
        return None

    def _submit(self, scene_prompt: str, reference_image_path: str) -> str:
        """이미지 편집 작업을 제출하고 검증된 request_id를 반환한다.

        레퍼런스 이미지는 fal-storage에 먼저 업로드해 https file_url로 넘긴다(_upload_reference).
        대용량 base64 data URI는 Kontext가 레퍼런스를 못 읽고 검정 placeholder를 돌려주는
        정황이 라이브에서 관측됨 → https file_url로 안정화한다.

        PR #65 교훈: _send_json에 sleep=self._sleep + max_transient_retries 적용 —
        제출 직후 429 한 번에 파이프라인이 죽지 않도록 transient 재시도한다.
        """
        ref_bytes = _read_bytes(reference_image_path, "Kontext 레퍼런스 이미지")
        mime = _guess_image_mime(reference_image_path)
        image_url = self._upload_reference(ref_bytes, mime, Path(reference_image_path).name)
        body = {
            "prompt": scene_prompt,
            "image_url": image_url,
            "aspect_ratio": "9:16",
            "output_format": "png",
            "num_images": 1,
        }
        url = f"{_FAL_QUEUE_BASE}/{self._model}"
        data = _send_json(
            lambda: self._client().post(url, headers=_fal_headers(self.settings), json=body),
            "Kontext 작업 제출",
            sleep=self._sleep,
            max_transient_retries=_MAX_TRANSIENT_RETRIES,
        )
        request_id = data.get("request_id")
        if not request_id:
            # redaction: 예외 메시지에 응답 키 목록 금지 — log.debug로만 기록한다.
            log.debug("kontext.submit.missing_request_id", keys=list(data.keys()))
            raise VideoRenderError("Kontext 응답에 request_id가 없습니다")
        return _validate_request_id(str(request_id))

    def _upload_reference(self, ref_bytes: bytes, mime: str, file_name: str) -> str:
        """레퍼런스 이미지를 fal-storage에 업로드하고 https file_url을 반환한다.

        ① initiate: POST {rest}/storage/upload/initiate?storage_type=fal-cdn-v3
           (Authorization: Key) → {"file_url", "upload_url"}. _send_json으로 429 재시도.
        ② PUT upload_url: presigned URL이므로 자격증명 미첨부, Content-Type만 붙인다.
        보안: upload_url/file_url은 신뢰 불가 입력이라 PUT/반환 전에 host를 검증한다(SSRF).
        redaction: 응답 키 목록은 log.debug로만, 예외 메시지에 금지.
        """
        initiate_url = f"{_FAL_REST_BASE}/storage/upload/initiate?storage_type=fal-cdn-v3"
        data = _send_json(
            lambda: self._client().post(
                initiate_url,
                headers=_fal_headers(self.settings),
                json={"content_type": mime, "file_name": file_name},
            ),
            "Kontext 레퍼런스 업로드 개시",
            sleep=self._sleep,
            max_transient_retries=_MAX_TRANSIENT_RETRIES,
        )
        file_url = data.get("file_url")
        upload_url = data.get("upload_url")
        if not file_url or not upload_url:
            # redaction: 예외 메시지에 응답 키 목록 금지 — log.debug로만 기록.
            log.debug("kontext.upload.missing_url", keys=list(data.keys()))
            raise VideoRenderError("Kontext 레퍼런스 업로드 응답에 URL이 없습니다")
        upload_url = str(upload_url)
        file_url = str(file_url)
        # 두 URL 모두 PUT(왕복) 전에 검증한다(신뢰 불가 입력 — SSRF). presigned PUT 대상은
        # fal 스토리지(클라우드 presigned일 수 있음)라 https + 사설망 차단만 강제하고
        # 자격증명은 붙이지 않는다(쿼리스트링에 서명이 들어있음). file_url은 이후 image_url로
        # fal에 다시 넘어가므로 허용 CDN 호스트인지 미리 검증해 불필요한 PUT 왕복을 막는다.
        _validate_upload_url(upload_url)
        _validate_kontext_image_url(file_url)
        self._put_reference(upload_url, ref_bytes, mime)
        return file_url

    def _put_reference(self, upload_url: str, ref_bytes: bytes, mime: str) -> None:
        """presigned upload_url에 레퍼런스 바이트를 PUT한다(일시 오류 backoff 재시도).

        initiate(POST)와 동일하게 429/5xx는 최대 _MAX_TRANSIENT_RETRIES회 재시도한다 —
        업로드 한 번의 일시 오류로 프레임 생성 전체가 죽지 않도록(PR #65 교훈, _status_once
        와 동일 backoff). 자격증명은 미첨부(Content-Type만). 응답은 JSON이 아니므로
        _raise_for_status로 상태만 검증한다.
        """
        attempts = 0
        while True:
            resp = _safe_send(
                lambda: self._client().put(
                    upload_url, content=ref_bytes, headers={"Content-Type": mime}
                ),
                "Kontext 레퍼런스 업로드",
            )
            code = getattr(resp, "status_code", None)
            transient = isinstance(code, int) and (code == 429 or code >= 500)
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                self._sleep(_RETRY_BACKOFF_SEC * (2 ** (attempts - 1)))
                continue
            _raise_for_status(resp, "Kontext 레퍼런스 업로드")
            return

    def _poll(self, request_id: str) -> str:
        """상태를 COMPLETED까지 폴링하고, 결과에서 검증된 이미지 URL을 반환한다.

        경계는 `< timeout`(off-by-one 방지). 폴링/결과 URL은 request_id로 직접 구성.
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
            raise VideoRenderError(f"Kontext 작업 실패: status={status}")
        raise VideoTimeoutError(
            f"Kontext 폴링 타임아웃({self._timeout:.0f}s, 폴링 {self.poll_count}회)"
        )

    def _status_once(self, url: str) -> tuple[dict, float]:
        """상태 1회 조회. 일시 오류(429/5xx)는 지수 backoff로 최대 3회 재시도.

        반환은 (응답 dict, 재시도 backoff 합계 초) — backoff는 호출부가 timeout에 누적.
        FalVeoClient._status_once와 동일 로직.
        """
        attempts = 0
        backoff_total = 0.0
        while True:
            self.poll_count += 1
            resp = _safe_send(
                lambda: self._client().get(url, headers=_fal_headers(self.settings)),
                "Kontext 상태 조회",
            )
            code = getattr(resp, "status_code", None)
            transient = isinstance(code, int) and (code == 429 or code >= 500)
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                wait = _RETRY_BACKOFF_SEC * (2 ** (attempts - 1))
                self._sleep(wait)
                backoff_total += wait
                continue
            from nutti.integrations.video import _json_or_raise

            return _json_or_raise(resp, "Kontext 상태 조회"), backoff_total

    def _fetch_result_url(self, request_id: str) -> str:
        """완료된 작업의 결과에서 검증된 이미지 URL을 방어적으로 추출·반환한다.

        결과 스키마: {"images": [{"url": "..."}]}.

        PR #65 교훈: _send_json에 sleep + max_transient_retries 적용 —
        이미지 생성이 끝난 뒤(과금 완료) 결과 조회 429로 손실 없도록.
        redaction: 응답 키 목록은 log.debug로만, 예외 메시지에 금지.
        """
        result_url = f"{_FAL_QUEUE_BASE}/{self._app_id}/requests/{request_id}"
        data = _send_json(
            lambda: self._client().get(result_url, headers=_fal_headers(self.settings)),
            "Kontext 결과 조회",
            sleep=self._sleep,
            max_transient_retries=_MAX_TRANSIENT_RETRIES,
        )
        images = data.get("images")
        uri: str | None = None
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict):
                uri = first.get("url")
        if not uri:
            # redaction: 예외 메시지에 응답 키 목록 금지 — log.debug로만 기록.
            log.debug("kontext.result.missing_url", keys=list(data.keys()))
            raise VideoRenderError("Kontext 결과에 이미지 URL이 없습니다")
        uri = str(uri)
        _validate_kontext_image_url(uri)
        return uri

    def _download(self, uri: str) -> str:
        """검증된 이미지 URL에서 바이트를 내려받아 media_dir에 저장하고 경로를 반환한다.

        CDN(fal.media / v3.fal.media)은 키 없이 내려받는다(자격증명 헤더 미첨부).
        리다이렉트는 한 hop만 허용하고 그 Location도 호스트를 재검증한다(SSRF 체인 차단).
        저장 파일명: frame_{uuid12}.{ext}. ext는 Content-Type에서 png/jpg, 기본 png.
        """
        _validate_kontext_image_url(uri)
        resp = _safe_send(
            lambda: self._client().get(uri, follow_redirects=False),
            "Kontext 이미지 다운로드",
        )
        sc = getattr(resp, "status_code", None)
        if not isinstance(sc, int):
            raise VideoRenderError("Kontext 이미지 다운로드 응답에 유효한 status_code가 없습니다")
        if 300 <= sc < 400:
            location = (getattr(resp, "headers", {}) or {}).get("location", "")
            if not location:
                raise VideoRenderError(
                    "Kontext 이미지 다운로드: 리다이렉트 응답에 Location 헤더 없음"
                )
            _validate_kontext_image_url(location)
            resp = _safe_send(
                lambda: self._client().get(location, follow_redirects=False),
                "Kontext 이미지 다운로드(리다이렉트)",
            )
            r_sc = getattr(resp, "status_code", None)
            if isinstance(r_sc, int) and 300 <= r_sc < 400:
                raise VideoRenderError("Kontext 다운로드: 허용 호스트 이후 추가 리다이렉트 금지")
        _raise_for_status(resp, "Kontext 이미지 다운로드")
        content = getattr(resp, "content", None)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise VideoRenderError("Kontext 다운로드 응답에 이미지 바이트가 없습니다")
        # Content-Type에서 확장자를 결정한다. 기본 png.
        ext = _ext_from_content_type(
            (getattr(resp, "headers", {}) or {}).get("content-type", "")
        )
        out_path = Path(self.settings.nutti_media_dir) / f"frame_{uuid4().hex[:12]}.{ext}"
        _write_bytes(out_path, bytes(content), "Kontext 이미지")
        log.info("kontext.frame.saved", path=str(out_path))
        return str(out_path)


def _validate_kontext_image_url(url: str) -> None:
    """결과 이미지 다운로드 URL이 허용된 fal CDN 호스트인지 검증한다(SSRF 방어).

    _validate_fal_video_url과 동일한 원칙: scheme=https + host가 _FAL_SAFE_HOSTS
    (fal.media, fal.run) 또는 그 서브도메인이어야 한다. suffix 매칭이라 Kontext 결과
    호스트인 v3.fal.media 등 *.fal.media 서브도메인이 자동 포함된다.
    API 응답값은 신뢰 불가 입력이므로 다운로드 전에 반드시 검증한다.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise VideoRenderError("Kontext 다운로드: 이미지 URL scheme 불허 (허용: https)")
    host = (parsed.hostname or "").lower()
    if not any(host == s or host.endswith(f".{s}") for s in _FAL_SAFE_HOSTS):
        raise VideoRenderError(
            "Kontext 다운로드: 이미지 URL 호스트 불허 (허용: fal.media, fal.run 및 서브도메인)"
        )


def _validate_upload_url(url: str) -> None:
    """presigned 업로드 URL(PUT 대상)을 검증한다(SSRF 방어).

    file_url과 달리 upload_url은 fal 클라우드 스토리지의 presigned URL이라 호스트가
    fal.media가 아닐 수 있다(예: 오브젝트 스토리지). 따라서 호스트를 화이트리스트로
    제한하지 않되, ① scheme=https ② 호스트가 loopback/사설/링크로컬 IP가 아님은 강제해
    내부망 접근을 차단한다. 이 URL은 인증된 initiate 응답에서만 오므로 출처 신뢰도는 높다.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise VideoRenderError("Kontext 업로드: URL scheme 불허 (허용: https)")
    host = (parsed.hostname or "").lower()
    if not host:
        raise VideoRenderError("Kontext 업로드: URL 호스트가 비어 있습니다")
    # IP 리터럴이면 사설/loopback/링크로컬/예약 대역 차단(클라우드 메타데이터 169.254.x 포함).
    try:
        import ipaddress

        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise VideoRenderError("Kontext 업로드: 사설/loopback IP 불허")
        return
    # 도메인명이면 내부망 suffix를 차단한다 — IP 검사만으로는 metadata.google.internal,
    # *.internal, *.local 같은 클라우드 내부 SSRF 경로를 못 막기 때문(리뷰 지적).
    if host == "localhost" or any(
        host == s or host.endswith(f".{s}")
        for s in ("localhost", "internal", "local", "metadata")
    ):
        raise VideoRenderError("Kontext 업로드: 사설/내부망 호스트 불허")


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """PNG 바이트에서 (width, height)를 추출한다. PNG가 아니거나 파싱 불가면 None.

    PNG는 8바이트 시그니처 다음 IHDR 청크가 오고, width/height가 각각 오프셋 16/20에
    big-endian uint32로 들어 있다(고정 위치). 외부 디코딩 라이브러리 없이 헤더만 읽는다.
    """
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _ext_from_content_type(content_type: str) -> str:
    """Content-Type 헤더에서 파일 확장자를 추출한다.

    `image/png` → `png`, `image/jpeg` → `jpg`, 그 외 → `png`(기본).
    """
    if not isinstance(content_type, str):
        return "png"
    ct = content_type.lower().split(";")[0].strip()
    if ct == "image/jpeg" or ct == "image/jpg":
        return "jpg"
    return "png"
