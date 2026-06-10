"""립싱크 영상 백엔드(Hedra Character-3) — 마스코트가 음성에 맞춰 실제로 입을 움직인다.

kling 보이스오버 백엔드(`video_kling.py`)는 마스코트가 **말하지 않고** 내레이션
위에서 움직이기만 하지만, 이 백엔드는 정반대로 **음성으로 마스코트의 입을 구동**한다.
흐름(비트별): ① ElevenLabs TTS(또는 주입 fake)로 한국어 아이 목소리 합성 →
② Hedra Character-3에 시작 프레임(이미지) + 음성(오디오)을 올려 립싱크 영상을 생성 →
③ Hedra 출력에 음성이 이미 입혀져 있으므로(mux 불필요) 다운로드한 클립을 그대로 사용.
비트들은 VideoStudio._stitch가 잇는다.

Hedra 공개 API(REST):
- 자산 생성: `POST /assets`(name·type) → asset_id, 업로드: `POST /assets/{id}/upload`(multipart file).
- 생성 제출: `POST /generations`(type=video·ai_model_id·start_keyframe_id·audio_id·옵션).
- 상태 폴링: `GET /generations/{id}/status` → status(complete/error/processing/queued/finalizing)
  와 download_url/url(완료 시). 출력은 음성 내장 MP4(별도 mux 불요).
- 인증: `X-API-Key` 헤더(Hedra 호스트 요청에만 첨부 — CDN 유출 방지).

계약(video.py·video_kling.py와 동일):
- 모든 오류는 `VideoRenderError`(폴링 초과는 `VideoTimeoutError`)로만 전파.
- redaction: 메시지에 URL·generation id·응답 본문 금지(상태 코드·타입명만).
- 주입 가능(http=/sleep=, hedra_client=/tts_client=)으로 네트워크 없이 테스트.
- `_HttpClosingMixin`으로 연결 풀 정리, `_write_bytes` 원자적 저장.
- API 응답값(generation id·다운로드 URL)은 신뢰 불가 입력 → 형식·호스트 검증(SSRF 방어).
  다운로드 허용 호스트는 기본 안전 호스트(api.hedra.com·hedra.com)에 더해, 라이브로
  확인된 CDN 호스트를 `NUTTI_HEDRA_DOWNLOAD_HOSTS`(콤마 구분 정확 호스트명)로 추가할 수
  있다 — 와일드카드 TLD는 파서가 제거해 SSRF 재유입을 막는다.
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
    _close_owned,
    _guess_image_mime,
    _HttpClosingMixin,
    _json_or_raise,
    _raise_for_status,
    _read_bytes,
    _safe_send,
    _sanitize_prompt_text,
    _send_json,
    _write_bytes,
)
from nutti.integrations.tts_elevenlabs import ElevenLabsTtsClient
from nutti.integrations.video_kling import (
    _unlink_quiet,
    _validate_model_id,
)
from nutti.logging import get_logger

log = get_logger(__name__)

# Hedra 공개 API 베이스. 자산·생성·상태 모두 이 호스트(자격증명 헤더는 여기에만 붙인다).
_HEDRA_BASE = "https://api.hedra.com/web-app/public"
# Hedra API 호스트(다운로드 URL의 자격증명 격리 판정용).
_HEDRA_API_HOST = "api.hedra.com"
# 결과 영상 다운로드를 허용하는 호스트(신뢰 불가 응답 URL의 SSRF 방어).
# Hedra는 download_url을 자체 도메인 또는 S3/CloudFront로 줄 수 있으나(문서상 호스트
# 미명시), `amazonaws.com`·`cloudfront.net` 같은 TLD 단위 허용은 공격자 소유 버킷
# (예: attacker-bucket.s3.amazonaws.com)까지 통과시켜 SSRF 방어를 무력화한다.
# 안전 폴백은 "미확인 호스트 차단"이므로, 라이브로 확인된 Hedra 자체 도메인만 좁게
# 허용한다. 실제 CDN 서브도메인은 키 확보 후 그 정확한 호스트만 추가한다.
#
# 실제 CDN 호스트는 라이브 API 응답으로 관측해야 알 수 있고(키 미확보), 코드 수정 없이
# 운영에서 추가할 수 있어야 한다. 그래서 정확한 호스트를 환경설정
# (`NUTTI_HEDRA_DOWNLOAD_HOSTS`, 콤마 구분)으로 **추가**할 수 있게 했다. 추가 호스트는
# `_parse_extra_hosts`가 정확 호스트명만 통과시킨다 — `*`/`/`/스킴/공백이 든 항목은
# 무시한다(와일드카드 TLD 허용으로 인한 SSRF 재유입 차단). 추가 시 반드시 라이브로
# 관측한 Hedra 전용 서브도메인만 넣어야 한다(공유 버킷 호스트 금지):
#   - 안전 예: 고정 CloudFront 배포 dXXXX.cloudfront.net(Hedra 전용 확인 시)
#   - 금지 예: hedra-prod.s3.amazonaws.com 같은 공유 테넌트 버킷 호스트
_HEDRA_SAFE_HOSTS = frozenset({"hedra.com", "api.hedra.com"})


def _parse_extra_hosts(raw: str) -> frozenset[str]:
    """설정에서 받은 추가 허용 호스트 문자열을 정확 호스트명 집합으로 파싱한다.

    `NUTTI_HEDRA_DOWNLOAD_HOSTS`는 콤마로 구분한 정확한 호스트명 목록이다
    (예: "files.hedra.com,d1234abcd.cloudfront.net"). 신뢰 불가 운영 입력으로 보아,
    호스트명 형식이 아닌 항목은 조용히 버린다 — 특히 `*` 와일드카드·`/`(경로)·`:`(포트/스킴)
    ·공백이 든 값을 거부해 TLD 단위 허용으로 인한 SSRF 재유입을 막는다. 정규화는 소문자.
    """
    out: set[str] = set()
    for part in (raw or "").split(","):
        host = part.strip().lower()
        if not host:
            continue
        # 호스트명만 허용: 영숫자·`-`·`.`로 구성, 와일드카드/경로/포트/스킴/공백은 거부.
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            continue
        # 선행/후행 점이나 빈 라벨(`a..b`)은 호스트명이 아니다.
        if host.startswith(".") or host.endswith(".") or ".." in host:
            continue
        out.add(host)
    return frozenset(out)


def _hedra_allowed_hosts(settings: Settings) -> frozenset[str]:
    """이 설정에서 다운로드를 허용할 호스트 집합(기본 안전 호스트 + 설정 추가분)."""
    extra = _parse_extra_hosts(getattr(settings, "hedra_download_hosts", "") or "")
    return _HEDRA_SAFE_HOSTS | extra
# Hedra asset/generation id 허용 형태(폴링·다운로드 URL에 삽입 전 검증). UUID 꼴이지만
# 신뢰 불가 응답값이므로 영숫자·`-`·`_`만 허용한다(Kling _REQUEST_ID_RE와 동일 원칙).
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_RESOURCE_ID_CHARS = 128
# 프롬프트에 삽입하는 AI 생성 텍스트 길이 상한(주입 표면 제한, kling과 동일).
_MAX_SCENE_CHARS = 500
# 폴링 중 일시 오류(429/5xx) 최대 재시도와 backoff 기준(초). video.py와 동일 원칙.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_SEC = 2.0


def _hedra_headers(settings: Settings) -> dict:
    """Hedra 인증 헤더(`X-API-Key`). Hedra API 호스트 요청에만 붙인다.

    이 헤더는 자격증명이므로 **api.hedra.com 요청에만** 사용한다 — 결과 영상은
    Hedra가 돌려준 download_url(자체 도메인 또는 CDN)에서 받으므로, CDN 요청에
    키를 실으면 그 호스트의 로그/중간자에게 키가 샌다(Gemini·fal 키 격리와 동일 원칙).
    Content-Type은 JSON 요청에만 의미가 있어 호출부가 multipart 업로드에선 빼고 쓴다.
    """
    return {"X-API-Key": settings.hedra_api_key, "Content-Type": "application/json"}


def _validate_resource_id(resource_id: str, *, what: str) -> str:
    """Hedra asset/generation id가 폴링·다운로드 URL에 안전하게 삽입 가능한지 검증한다.

    제출/자산 응답의 id를 `{base}/generations/{id}/status` 등으로 이어 붙이므로
    신뢰 불가 입력으로 본다. 허용 문자(영숫자·`-`·`_`)·길이만 통과(원문은 미노출,
    길이만 진단). Kling의 _validate_request_id와 동일 계약.
    """
    rid = (resource_id or "").strip()
    if not rid or len(rid) > _MAX_RESOURCE_ID_CHARS or not _RESOURCE_ID_RE.match(rid):
        raise VideoRenderError(f"Hedra {what} id 형식이 올바르지 않습니다 (길이 {len(resource_id or '')})")
    return rid


def _validate_hedra_download_url(url: str, allowed_hosts: frozenset[str] | None = None) -> None:
    """결과 영상 다운로드 URL이 허용된 호스트인지 검증한다(SSRF 방어).

    scheme=https + host가 허용 호스트(또는 그 서브도메인)여야 한다. `allowed_hosts`를
    주면 그 집합으로, 없으면 기본 안전 호스트(_HEDRA_SAFE_HOSTS)로 검증한다 —
    운영에서 라이브 확인된 CDN 호스트를 설정으로 추가할 때 클라이언트가 합집합을 넘긴다.
    API 응답값(download_url)은 신뢰 불가 입력이므로 다운로드 전에 검증한다.
    """
    hosts = allowed_hosts if allowed_hosts is not None else _HEDRA_SAFE_HOSTS
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise VideoRenderError("Hedra 다운로드: 영상 URL scheme 불허 (허용: https)")
    host = (parsed.hostname or "").lower()
    if not any(host == s or host.endswith(f".{s}") for s in hosts):
        raise VideoRenderError("Hedra 다운로드: 영상 URL 호스트 불허")


class LipsyncPromptBuilder:
    """립싱크 클립용 프롬프트 빌더 — 마스코트가 **음성에 맞춰 입을 움직이며 말한다**.

    kling 보이스오버(KlingPromptBuilder)는 "입을 다물고 말하지 말 것"(_NO_SPEAK)을
    명시하지만, 립싱크는 정반대로 ① 음성에 맞춰 입을 움직이며 말하고 ② 화면 자막은
    여전히 금지(깨진 한글 자막 방지) ③ 추가 동물·사람 금지를 명시한다. 비트 텍스트
    (대사 내용)는 장면 톤 힌트로만 쓰고 `_sanitize_prompt_text`로 정제한다(주입 방어).
    """

    # =========================== PO 수정 구역 (립싱크 연출) ===========================
    # 립싱크 영상의 "움직임·카메라·금지요소"를 바꾸려면 아래 영어 템플릿을 고친다.
    # 마스코트는 음성에 맞춰 입을 움직이며 말한다(_SPEAK) — kling과 반대다.
    _MOTION = (
        "A photorealistic dog mascot in a cozy warmly lit studio, talking to the camera "
        "with friendly, lively expression, subtle natural head movements and blinking."
    )
    _CAMERA = "Camera: locked-off tripod, medium close-up, eye-level, no camera movement."
    _SPEAK = (
        "The dog moves its mouth naturally to lip-sync the provided voice audio, "
        "as if it is actually speaking."
    )
    _NEGATIVE = (
        "Strictly no additional animals, no people. Absolutely no text, subtitles, captions, "
        "letters, words, or writing anywhere in the frame."
    )
    # ========================= PO 수정 구역 끝 (립싱크 연출) =========================

    def build_beat(self, beat_text: str) -> str:
        """비트 1개의 립싱크 프롬프트를 만든다(입을 움직여 말하는 마스코트 + 자막 금지)."""
        mood = _sanitize_prompt_text(beat_text.strip() or "", _MAX_SCENE_CHARS)
        return (
            f"{self._MOTION} Scene mood (do not render as text): '{mood}'. "
            f"{self._CAMERA} {self._SPEAK} "
            "Format: vertical 9:16. "
            f"{self._NEGATIVE}"
        )


class HedraLipsyncClient(_HttpClosingMixin):
    """Hedra Character-3 립싱크 클라이언트(자산 업로드 → 제출 → 폴링 → 다운로드).

    Hedra 공개 REST: ① 이미지·오디오를 각각 `POST /assets`(생성) +
    `POST /assets/{id}/upload`(multipart)로 올려 asset_id를 받고 →
    ② `POST /generations`에 start_keyframe_id(이미지)·audio_id(오디오)·프롬프트·9:16을
    제출해 generation_id를 받은 뒤 → ③ `GET /generations/{id}/status`를 interval·timeout
    한도까지 폴링 → ④ status=complete면 응답의 download_url을 검증·다운로드해 media_dir에
    저장한다. 출력은 음성 내장 MP4라 별도 mux가 없다.

    오류 계약: HTTP·전송·JSON·쓰기 실패는 VideoRenderError, 폴링 초과는
    VideoTimeoutError. 일시 오류(429/5xx)는 backoff로 최대 3회 재시도.
    자격증명(X-API-Key)은 api.hedra.com 요청에만, CDN 다운로드엔 붙이지 않는다.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep if sleep is not None else time.sleep
        # 설정값(lipsync_model)은 생성 본문(ai_model_id)에 들어가므로 형식을 검증한다.
        self._model = _validate_model_id(settings.lipsync_model, env_name="NUTTI_LIPSYNC_MODEL")
        self._interval = float(settings.lipsync_poll_interval_sec)
        if self._interval <= 0:
            raise ValueError(f"lipsync_poll_interval_sec는 0보다 커야 합니다(현재 {self._interval})")
        self._timeout = float(settings.lipsync_timeout_sec)
        if self._timeout <= 0:
            raise ValueError(f"lipsync_timeout_sec는 0보다 커야 합니다(현재 {self._timeout})")
        # 다운로드 허용 호스트 = 기본 안전 호스트 + 설정 추가분(라이브 확인된 CDN 호스트).
        # 신뢰 불가 응답 URL의 SSRF 검증에 쓴다(생성 시 1회 계산, 와일드카드는 파서가 제거).
        self._allowed_hosts = _hedra_allowed_hosts(settings)
        self.poll_count = 0

    def _client(self):
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    def generate(self, frame_path: str, voice_path: str, prompt: str) -> str:
        """시작 프레임 + 음성 + 프롬프트로 립싱크 영상을 생성하고 로컬 저장 경로를 반환한다."""
        image_id = self._upload_asset(frame_path, "image", _guess_image_mime(frame_path))
        audio_id = self._upload_asset(voice_path, "audio", "audio/wav")
        generation_id = self._submit(image_id, audio_id, prompt)
        download_url = self._poll(generation_id)
        return self._download(download_url)

    def _upload_asset(self, file_path: str, asset_type: str, content_type: str) -> str:
        """자산을 생성(POST /assets)하고 바이트를 업로드(POST /assets/{id}/upload)한다.

        ① name·type으로 자산 레코드를 만들어 검증된 asset_id를 받고 →
        ② 그 id로 multipart 업로드한다. 두 요청 모두 Hedra 호스트라 X-API-Key를 붙인다
        (multipart 업로드는 Content-Type을 httpx가 boundary와 함께 설정하므로 JSON
        헤더의 Content-Type은 빼고 보낸다). 업로드 응답 상태만 검증(본문은 쓰지 않음).
        """
        create_url = f"{_HEDRA_BASE}/assets"
        body = {"name": f"nutti_{uuid4().hex[:12]}", "type": asset_type}
        data = _send_json(
            lambda: self._client().post(create_url, headers=_hedra_headers(self.settings), json=body),
            f"Hedra 자산 생성({asset_type})",
        )
        asset_id = data.get("id") or data.get("asset_id")
        if not asset_id:
            log.debug("hedra.asset.missing_id", keys=list(data.keys()), asset_type=asset_type)
            raise VideoRenderError(f"Hedra 자산 생성 응답에 id가 없습니다 (응답 키: {list(data.keys())})")
        asset_id = _validate_resource_id(str(asset_id), what="자산")

        file_bytes = _read_bytes(file_path, f"Hedra {asset_type} 자산")
        upload_url = f"{_HEDRA_BASE}/assets/{asset_id}/upload"
        # multipart 업로드: Content-Type(boundary)은 httpx가 자동 설정하므로 X-API-Key만.
        upload_headers = {"X-API-Key": self.settings.hedra_api_key}
        resp = _safe_send(
            lambda: self._client().post(
                upload_url,
                headers=upload_headers,
                files={"file": (Path(file_path).name, file_bytes, content_type)},
            ),
            f"Hedra 자산 업로드({asset_type})",
        )
        _raise_for_status(resp, f"Hedra 자산 업로드({asset_type})")
        return asset_id

    def _submit(self, image_id: str, audio_id: str, prompt: str) -> str:
        """립싱크 생성 작업을 제출하고 검증된 generation_id를 반환한다."""
        body = {
            "type": "video",
            "ai_model_id": self._model,
            "start_keyframe_id": image_id,
            "audio_id": audio_id,
            "generated_video_inputs": {
                "text_prompt": prompt,
                "aspect_ratio": "9:16",
                "resolution": "720p",
            },
        }
        url = f"{_HEDRA_BASE}/generations"
        data = _send_json(
            lambda: self._client().post(url, headers=_hedra_headers(self.settings), json=body),
            "Hedra 작업 제출",
        )
        generation_id = data.get("id") or data.get("generation_id")
        if not generation_id:
            log.debug("hedra.submit.missing_id", keys=list(data.keys()))
            raise VideoRenderError(f"Hedra 응답에 generation id가 없습니다 (응답 키: {list(data.keys())})")
        return _validate_resource_id(str(generation_id), what="생성")

    def _poll(self, generation_id: str) -> str:
        """상태를 complete까지 폴링하고, 응답의 검증된 다운로드 URL을 반환한다.

        경계는 `< timeout`(off-by-one 방지). 폴링 URL은 generation_id로 직접 구성한다.
        status=error면 명시적으로 실패(무한 폴링 방지). complete면 download_url(없으면
        url)을 SSRF 검증해 반환한다.
        """
        status_url = f"{_HEDRA_BASE}/generations/{generation_id}/status"
        elapsed = 0.0
        while elapsed < self._timeout:
            data, backoff_sec = self._status_once(status_url)
            elapsed += backoff_sec
            status = data.get("status")
            if status == "complete":
                return self._extract_download_url(data)
            if status == "error":
                # error_message는 내부 상세가 박힐 수 있어 노출하지 않는다(redaction).
                raise VideoRenderError("Hedra 작업 실패: status=error")
            if status in ("processing", "queued", "finalizing", None):
                self._sleep(self._interval)
                elapsed += self._interval
                continue
            # 알 수 없는 종료 상태 — 명시적으로 실패한다.
            raise VideoRenderError(f"Hedra 작업 실패: status={status}")
        raise VideoTimeoutError(f"Hedra 폴링 타임아웃({self._timeout:.0f}s, 폴링 {self.poll_count}회)")

    def _status_once(self, url: str) -> tuple[dict, float]:
        """상태 1회 조회. 일시 오류(429/5xx)는 지수 backoff로 최대 3회 재시도.

        반환은 (응답 dict, 재시도 backoff 합계 초) — backoff는 호출부가 timeout에 누적.
        Kling._status_once와 동일 패턴.
        """
        attempts = 0
        backoff_total = 0.0
        while True:
            self.poll_count += 1
            resp = _safe_send(
                lambda: self._client().get(url, headers=_hedra_headers(self.settings)),
                "Hedra 상태 조회",
            )
            code = getattr(resp, "status_code", None)
            transient = isinstance(code, int) and (code == 429 or code >= 500)
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                wait = _RETRY_BACKOFF_SEC * (2 ** (attempts - 1))
                self._sleep(wait)
                backoff_total += wait
                continue
            return _json_or_raise(resp, "Hedra 상태 조회"), backoff_total

    def _extract_download_url(self, data: dict) -> str:
        """완료 응답에서 다운로드 URL을 방어적으로 추출·검증해 반환한다.

        Hedra 상태 응답은 download_url(우선)·url·streaming_url을 줄 수 있다. 직접
        인덱싱하지 않고 download_url → url 순으로 찾고, SSRF 호스트 검증을 통과시킨다.
        """
        uri = data.get("download_url") or data.get("url")
        if not uri:
            log.debug("hedra.poll.missing_url", keys=list(data.keys()))
            raise VideoRenderError(f"Hedra 완료 응답에 영상 URL이 없습니다 (응답 키: {list(data.keys())})")
        uri = str(uri)
        _validate_hedra_download_url(uri, self._allowed_hosts)
        return uri

    def _download(self, uri: str) -> str:
        """검증된 영상 URL에서 바이트를 내려받아 media_dir에 저장하고 경로를 반환한다.

        다운로드 CDN은 키 없이 받는다(자격증명 헤더 미첨부) — download_url이
        api.hedra.com이 아니면 키를 빼서 CDN/중간자에 새지 않게 한다(키 격리).
        리다이렉트는 한 hop만 허용하고 그 Location도 호스트를 재검증한다(SSRF 체인 차단).
        """
        _validate_hedra_download_url(uri, self._allowed_hosts)
        # api.hedra.com 직링크일 때만 키를 붙이고, CDN(S3/CloudFront)엔 붙이지 않는다.
        host = (urlparse(uri).hostname or "").lower()
        is_hedra_api = host == _HEDRA_API_HOST or host.endswith(f".{_HEDRA_API_HOST}")
        headers = {"X-API-Key": self.settings.hedra_api_key} if is_hedra_api else None
        resp = _safe_send(
            lambda: self._client().get(uri, headers=headers, follow_redirects=False),
            "Hedra 영상 다운로드",
        )
        sc = getattr(resp, "status_code", None)
        if not isinstance(sc, int):
            raise VideoRenderError("Hedra 영상 다운로드 응답에 유효한 status_code가 없습니다")
        if 300 <= sc < 400:
            location = (getattr(resp, "headers", {}) or {}).get("location", "")
            if not location:
                raise VideoRenderError("Hedra 영상 다운로드: 리다이렉트 응답에 Location 헤더 없음")
            _validate_hedra_download_url(location, self._allowed_hosts)
            # 리다이렉트 대상도 api.hedra.com이 아니면 키를 빼고 재요청한다(키 격리).
            r_host = (urlparse(location).hostname or "").lower()
            r_is_hedra = r_host == _HEDRA_API_HOST or r_host.endswith(f".{_HEDRA_API_HOST}")
            redir_headers = {"X-API-Key": self.settings.hedra_api_key} if r_is_hedra else None
            resp = _safe_send(
                lambda: self._client().get(location, headers=redir_headers, follow_redirects=False),
                "Hedra 영상 다운로드(리다이렉트)",
            )
            r_sc = getattr(resp, "status_code", None)
            if isinstance(r_sc, int) and 300 <= r_sc < 400:
                raise VideoRenderError("Hedra 다운로드: 허용 호스트 이후 추가 리다이렉트 금지")
        _raise_for_status(resp, "Hedra 영상 다운로드")
        content = getattr(resp, "content", None)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise VideoRenderError("Hedra 다운로드 응답에 영상 바이트가 없습니다")
        out_path = Path(self.settings.nutti_media_dir) / f"lipsync_{uuid4().hex[:12]}.mp4"
        _write_bytes(out_path, bytes(content), "Hedra 영상")
        log.info("lipsync.video.saved", path=str(out_path))
        return str(out_path)


class LipsyncBackend:
    """비트별 [ElevenLabs TTS → Hedra 립싱크 영상] 클립 리스트를 만드는 백엔드.

    VideoStudio가 dry_run이 아닐 때 lipsync 백엔드로 호출한다. 클라이언트는 주입
    가능(테스트 fake)하며, 미주입 시 실 경로에서 지연 생성하고 finally에서 정확히
    1회 닫는다(연결 풀 누수 방지). Hedra 출력은 음성 내장 MP4라 kling과 달리 mux가
    없다 — 비트 클립 = 다운로드한 립싱크 영상 그 자체다.
    """

    def __init__(self, settings: Settings, *, lipsync_client=None, tts_client=None, sleep=None):
        self.settings = settings
        self._lipsync_client = lipsync_client
        self._tts_client = tts_client
        self._sleep = sleep

    def produce_beat_clips(self, frame_path: str, beats: list[str]) -> tuple[list[str], float]:
        """각 비트를 [음성 합성 → 입 움직이는 립싱크 영상] 클립으로 만들어 (경로들, 총길이초)를 반환한다.

        총길이초는 각 비트의 합성 음성 길이(audio_sec) 합이다 — Hedra 출력 길이는
        입력 오디오 길이에 맞춰지므로(립싱크), 클립 길이 ≈ audio_sec다. kling처럼
        실측 합계를 함께 돌려줘 상위(VideoStudio)가 duration_sec을 정확히 채우게 한다.
        내레이션 WAV는 영상 생성 후 더 필요 없으므로 즉시 정리하고, 중도 실패 시 이미
        완성한 클립도 정리한 뒤 전파한다(수백 MB leak 방지 — kling 백엔드와 동일 원칙).
        """
        builder = LipsyncPromptBuilder()
        lipsync = self._lipsync_client
        tts = self._tts_client
        owned_lipsync = owned_tts = None
        if lipsync is None:
            lipsync = owned_lipsync = HedraLipsyncClient(self.settings, sleep=self._sleep)
        if tts is None:
            tts = owned_tts = ElevenLabsTtsClient(self.settings, sleep=self._sleep)
        clips: list[str] = []
        total_sec = 0.0
        try:
            for i, beat in enumerate(beats, start=1):
                voice_path, audio_sec = tts.synthesize(beat)
                try:
                    clip = lipsync.generate(frame_path, voice_path, builder.build_beat(beat))
                finally:
                    # 내레이션 WAV는 Hedra 업로드 후 더 필요 없으므로 즉시 정리한다
                    # (영상 생성 성공/실패 무관 — 수백 KB~MB 누적 방지).
                    _unlink_quiet(voice_path)
                # Hedra 출력 길이 ≈ 입력 음성 길이(립싱크) → audio_sec로 실측 합산.
                total_sec += float(audio_sec)
                log.info("video.lipsync.clip.done", path=clip, beat=i, of=len(beats))
                clips.append(clip)
        except BaseException:
            # 중도 실패: 이미 완성된 lipsync_*.mp4(각 수백 MB)가 영구 leak되지 않도록
            # 정리한 뒤 전파한다(현재 비트의 내레이션 WAV는 위 finally가 이미 처리).
            for done in clips:
                _unlink_quiet(done)
            raise
        finally:
            if owned_lipsync is not None:
                _close_owned(owned_lipsync)
            if owned_tts is not None:
                _close_owned(owned_tts)
        return clips, total_sec
