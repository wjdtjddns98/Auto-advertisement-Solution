"""영상 생성 연동: NanoBanana(Gemini 시작 프레임) → Veo 3.1 image-to-video 단일컷 8초.

흐름: ① NanoBananaClient가 마스코트 시작 프레임 이미지를 생성해 로컬 저장 →
② VeoPromptBuilder가 대사를 작은따옴표로 인용한 프롬프트를 만들고 →
③ VeoClient가 image-to-video 작업을 제출·폴링한 뒤 **완료 즉시** 영상을 내려받아
로컬에 저장한다(Veo 산출물은 48시간 후 삭제되므로 무음 유실 방지).

dry_run에서는 네트워크/키 없이 결정적 더미 경로를 채워 파이프라인을 검증한다.
모든 오류는 `VideoRenderError`(타임아웃은 `VideoTimeoutError`)로만 전파한다 —
HTTP 상태·전송·JSON 파싱·디스크 쓰기 실패 전부 포함(오케스트레이터 계약).
에러 메시지는 상태 코드/예외 타입명만 남기고 URL·operation id·응답 본문은
노출하지 않는다(redaction).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import Script, VideoAsset

log = get_logger(__name__)

# Gemini API 베이스 URL. 인증은 `x-goog-api-key` 헤더(Bearer 아님).
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# 리다이렉트 Location 헤더 판별에 사용하는 Gemini 호스트 prefix.
# /v1beta 뿐만 아니라 /download/v1beta 등 다른 경로로도 302가 올 수 있으므로
# 호스트 레벨에서 비교한다.
_GEMINI_HOST = "https://generativelanguage.googleapis.com"
# 폴링 중 일시 오류(429 쿼터·5xx 백엔드 장애)의 최대 재시도 횟수와 backoff 기준(초).
# 600s 폴링 윈도우에서 단 1회의 일시 오류로 작업을 영구 포기하지 않기 위한 장치다.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_SEC = 2.0
# 프롬프트에 삽입하는 AI 생성 텍스트의 길이 상한(주입 표면 제한).
# 단일컷 8초 영상의 대사/주제는 이 한도를 넘을 이유가 없다.
_MAX_DIALOGUE_CHARS = 500
_MAX_TOPIC_CHARS = 200
# Veo 제출 응답의 operation name 허용 형태. API 응답값을 그대로 폴링 URL에
# 끼워 넣으므로(_poll의 `{base}/{op_name}`) 신뢰할 수 없는 입력으로 보고 검증한다.
# 허용 문자(영숫자·`.`·`_`·`/`·`-`)만으로 구성돼야 하며, 이를 벗어나는 문자
# (`:` 스킴·`?`·`#` 쿼리/프래그먼트·`@`·공백·제어문자)는 요청 대상 변조(SSRF)·
# 쿼리 주입을 가능케 하므로 거부한다. `/operations/…`·`/tasks/…` 등 실 API가
# 어떤 경로 세그먼트를 쓰든(키 확보 전 미확정) 허용하되, 형태만 좁게 검증한다.
_OP_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_OP_NAME_CHARS = 256
# 302 리다이렉트 Location URL에서 허용할 호스트. _validate_op_name과 동일한
# 방어 원칙: API 응답값(Location 헤더)은 신뢰 불가 입력.
_SAFE_REDIRECT_HOSTS = frozenset(
    {"storage.googleapis.com", "generativelanguage.googleapis.com"}
)


def _validate_op_name(op_name: str) -> str:
    """Veo operation name이 폴링 URL에 안전하게 삽입 가능한 형태인지 검증한다.

    API 응답의 name을 `{_GEMINI_BASE}/{name}`으로 이어 붙이기 때문에, 이를
    신뢰하면 `:`(스킴)·`?`·`#`·`@` 등으로 요청 대상이 변조되거나 쿼리/프래그먼트
    주입이 가능하다(신뢰 불가 입력). ① 선행 슬래시를 정규화하고 ② 허용 문자
    (영숫자·`.`·`_`·`/`·`-`)만으로 구성됐는지, ③ 길이 상한을 넘지 않는지 확인한다.
    경로 세그먼트 이름(`operations`/`tasks` 등)은 실 API 미확정이라 강제하지
    않는다. 불일치 시 VideoRenderError(원문 미노출 — 길이만 진단)로 실패한다.
    """
    normalized = op_name.lstrip("/")
    if (
        not normalized
        or len(normalized) > _MAX_OP_NAME_CHARS
        or not _OP_NAME_RE.match(normalized)
    ):
        # 원문에는 내부 식별자가 있을 수 있어 길이만 노출(redaction).
        raise VideoRenderError(
            f"Veo operation name 형식이 올바르지 않습니다 (길이 {len(op_name)})"
        )
    return normalized


def _validate_redirect_location(location: str) -> None:
    """302 리다이렉트 Location URL이 안전한 호스트인지 검증한다.

    scheme=https + host가 _SAFE_REDIRECT_HOSTS 내에 있어야 한다.
    _validate_op_name과 동일 원칙: API 응답값은 신뢰 불가 입력(SSRF 방어).
    """
    parsed = urlparse(location)
    if parsed.scheme != "https":
        raise VideoRenderError("Veo 다운로드: Location URL scheme 불허 (허용: https)")
    host = (parsed.hostname or "").lower()
    if not any(host == s or host.endswith(f".{s}") for s in _SAFE_REDIRECT_HOSTS):
        raise VideoRenderError(
            "Veo 다운로드: Location URL 호스트 불허"
            " (허용: storage.googleapis.com, generativelanguage.googleapis.com)"
        )


def _sanitize_prompt_text(text: str, max_chars: int) -> str:
    """프롬프트에 삽입할 텍스트를 정제한다(간접 프롬프트 주입 방어).

    대본 본문/주제는 상위 AI 텍스트 파이프라인 산출물이라 이론상 신뢰
    가능하지만, 작은따옴표가 포함되면 VeoPromptBuilder의 인용 구분자를
    탈출해 임의 지시문(예: 안전 제약 무력화)을 이어 붙일 수 있다.
    ASCII 작은따옴표를 U+2019(오른쪽 따옴표)로 치환해 구분자 역할을
    제거하고 — Veo는 이를 자연어의 일부로 취급한다 — 길이를 제한해
    주입 표면을 한정한다(심층 방어).
    """
    return text.replace("'", "’").strip()[:max_chars]


def _usable_key(value: str | None) -> bool:
    """API 키 값이 실제로 쓸 수 있는지(비어 있지 않고 주석이 아님) 판정한다.

    pydantic-settings는 `.env`의 인라인 주석을 분리하지 않으므로,
    `KEY=   # 설명`처럼 빈 값 뒤에 주석이 붙으면 키 값이 `'# 설명'`이라는
    truthy 문자열로 파싱된다. 단순 truthiness 검사는 이런 더미 값을 진짜 키로
    오인해 fast-fail 가드를 우회시키므로, strip 후 주석(`#` 시작)을 배제한다.
    """
    if not value:
        return False
    stripped = value.strip()
    return bool(stripped) and not stripped.startswith("#")


def _close_http(http) -> None:
    """httpx 호환 클라이언트를 안전하게 닫는다(close가 있으면 호출).

    주입된 fake에는 close가 없을 수 있으므로 getattr로 방어한다.
    """
    if http is not None:
        close = getattr(http, "close", None)
        if callable(close):
            close()


def _close_owned(client) -> None:
    """VideoStudio가 자체 생성한 연동 클라이언트를 안전하게 닫는다.

    close()를 가진 실 클라이언트는 그걸 호출하고, close가 없는 대체 구현
    (테스트 monkeypatch가 반환하는 fake 등)은 조용히 건너뛴다.
    """
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        close()


class _HttpClosingMixin:
    """지연 생성한 `httpx.Client`(self._http)를 닫는 close/컨텍스트 매니저 제공.

    각 클라이언트는 self._http에 httpx.Client를 지연 캐싱하는데, 닫지 않으면
    장기 실행 스케줄러에서 TCP 연결 풀/파일 디스크립터가 누적된다. 이 믹스인은
    `close()`(멱등)와 `with` 지원을 더해 누수를 막는다. 주입받은 클라이언트도
    소유권이 호출부로 넘어온 것으로 보고 닫는다(호출부는 자체 생성분만 닫음).
    """

    _http = None

    def close(self) -> None:
        _close_http(self._http)
        self._http = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class VideoRenderError(RuntimeError):
    """영상 렌더(프레임 생성/제출/폴링/다운로드/저장) 실패. 영구 오류에 사용한다."""


class VideoTimeoutError(VideoRenderError):
    """렌더 작업이 폴링 제한 시간 안에 완료되지 않은 경우의 타임아웃."""


def _gemini_headers(settings: Settings) -> dict:
    """Gemini API 인증 헤더. `x-goog-api-key` 방식이다(Bearer 아님).

    이 헤더는 자격증명이므로 **Gemini API 도메인(_GEMINI_HOST) 요청에만** 붙인다 —
    외부 호스트(서명된 GCS URL 등)로 보내면 그 호스트의 액세스 로그/중간자에게
    키가 샌다(VeoClient._download 참조).
    """
    return {
        "x-goog-api-key": settings.gemini_api_key,
        "Content-Type": "application/json",
    }


def _raise_for_status(resp, what: str) -> None:
    """HTTP 4xx·5xx를 VideoRenderError로 전파(상태 코드만 노출, URL/본문 금지).

    status_code 속성이 없는 응답은 200으로 가정하면 잘못된 fake/응답을 조용히
    통과시켜 무음 결함을 만든다. 따라서 status_code가 없거나 int가 아니면
    명시적으로 VideoRenderError를 던져 분명히 실패시킨다(방어적 파싱).
    """
    code = getattr(resp, "status_code", None)
    if not isinstance(code, int):
        raise VideoRenderError(f"{what} 응답에 유효한 status_code가 없습니다")
    if code >= 400:
        raise VideoRenderError(f"{what} HTTP {code}")


def _safe_send(send, what: str):
    """전송 콜러블을 실행한다. 전송 계층 예외는 타입명만 남겨 VideoRenderError로.

    httpx 예외 문자열에는 전체 요청 URL(operation id 등 식별자 포함)이 박혀
    있을 수 있으므로 예외 타입명만 노출한다(redaction).
    """
    try:
        return send()
    except Exception as exc:  # noqa: BLE001 - 모든 전송 오류를 영구 렌더 오류로 승격
        raise VideoRenderError(f"{what} 통신 오류: {type(exc).__name__}") from None


def _json_or_raise(resp, what: str) -> dict:
    """응답을 검증하고 JSON dict를 반환한다(모든 실패는 VideoRenderError).

    `resp.json()` 자체가 실패할 수 있다 — CDN/프록시 장애로 HTTP 200에
    비-JSON 본문이 오면 json.JSONDecodeError가 난다. 이를 잡지 않으면
    VideoRenderError-only 계약이 깨지므로 별도로 감싼다.
    """
    _raise_for_status(resp, what)
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - 비-JSON 본문(200 포함) 방어
        raise VideoRenderError(f"{what} 응답 JSON 파싱 실패: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise VideoRenderError(f"{what} 응답이 JSON 객체(dict)가 아닙니다")
    return data


def _send_json(send, what: str) -> dict:
    """전송 + 상태 검증 + JSON 파싱을 한 번에 — 어떤 실패든 VideoRenderError로."""
    return _json_or_raise(_safe_send(send, what), what)


def _write_bytes(out_path: Path, data: bytes, what: str) -> None:
    """바이트를 디스크에 저장한다. OSError(디스크 풀/권한)도 VideoRenderError로.

    호출부(오케스트레이터)는 영상 서브시스템에서 VideoRenderError만 기대하므로,
    쓰기 실패를 그대로 새지 않게 한다. 메시지는 예외 타입명만(경로 노출 금지).
    """
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
    except OSError as exc:
        raise VideoRenderError(f"{what} 저장 실패: {type(exc).__name__}") from None


def _read_bytes(in_path: str, what: str) -> bytes:
    """로컬 파일을 읽는다. OSError는 VideoRenderError로 승격(타입명만 노출)."""
    try:
        return Path(in_path).read_bytes()
    except OSError as exc:
        raise VideoRenderError(f"{what} 읽기 실패: {type(exc).__name__}") from None


def _guess_image_mime(path: str) -> str:
    """확장자로 이미지 MIME 타입을 추정한다(.png → image/png, 그 외 jpeg)."""
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        return "image/png"
    return "image/jpeg"


class VeoPromptBuilder:
    """Veo 3.1 image-to-video 프롬프트 빌더(단일컷 8초·네이티브 한국어 음성).

    규칙(연구 노트 기반):
    - 대사는 작은따옴표로 인용해 Veo 네이티브 음성으로 발화시킨다(별도 TTS 불요).
    - 카메라는 고정(locked-off tripod)·medium close-up·eye-level — 흔들림/컷 전환 방지.
    - 깨짐 주원인(추가 동물·사람·화면 내 텍스트)을 명시적으로 금지한다.
    - 포맷: photorealistic · 9:16 세로 · single continuous 8-second shot.
    """

    def build(self, script: Script, *, off_screen_interviewer: bool = True) -> str:
        """대본에서 Veo 프롬프트를 만든다. 본문이 비면 주제로 폴백(빈 인용 방지).

        대사는 `_sanitize_prompt_text`로 정제한다 — 본문에 작은따옴표가 있으면
        인용 구분자를 탈출해 아래의 금지 지시(추가 동물·사람·텍스트 금지)를
        덮어쓰는 주입이 가능하기 때문이다.
        """
        dialogue = _sanitize_prompt_text(
            script.body.strip() or script.topic, _MAX_DIALOGUE_CHARS
        )
        speaking = (
            "speaking in Korean to an off-screen interviewer"
            if off_screen_interviewer
            else "speaking in Korean directly to the camera"
        )
        return (
            f"A photorealistic dog mascot {speaking}, saying: '{dialogue}'. "
            "Camera: locked-off tripod shot, medium close-up, eye-level, no camera movement. "
            "Format: vertical 9:16, single continuous 8-second shot. "
            "Strictly no additional animals, no people, no on-screen text, no captions."
        )


class NanoBananaClient(_HttpClosingMixin):
    """Gemini 이미지 생성(NanoBanana)으로 영상 시작 프레임을 만드는 클라이언트.

    실 경로(non-dry_run)에서만 생성되며, `httpx`는 메서드 안에서 lazy import한다.
    `POST /models/{model}:generateContent`에 텍스트 프롬프트(+선택적 레퍼런스
    이미지 inline_data)를 보내고, 응답의 이미지 파트를 base64 디코드해
    `settings.nutti_media_dir`에 저장한 뒤 로컬 경로를 반환한다.

    모든 오류(HTTP·전송·JSON 파싱·이미지 파트 누락·디스크 쓰기)는
    `VideoRenderError`로 전파한다. 테스트는 `http=`로 fake를 주입한다.
    """

    def __init__(self, settings: Settings, *, http=None):
        self.settings = settings
        self._http = http

    def _client(self):
        """httpx 클라이언트를 지연 확보(주입 우선). dry_run에선 호출되지 않는다."""
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    # Gemini 이미지 API는 간헐적으로 이미지 파트 없이 응답한다(알려진 flakiness).
    # 일시적 실패로 간주하고 최대 이 횟수만큼 재시도한다.
    _MAX_FRAME_RETRIES = 2

    def generate_frame(self, scene_prompt: str, *, reference_image_path: str | None = None) -> str:
        """시작 프레임 이미지를 생성해 media_dir에 저장하고 로컬 경로를 반환한다.

        `reference_image_path`가 있으면 마스코트 일관성을 위해 base64
        inline_data 파트로 첨부한다(이미지 컨디셔닝).
        Gemini가 이미지 파트 없이 응답하면 _MAX_FRAME_RETRIES 횟수만큼 재시도한다.
        """
        import base64
        import time

        parts: list[dict] = [{"text": scene_prompt}]
        if reference_image_path:
            ref_bytes = _read_bytes(reference_image_path, "마스코트 레퍼런스 이미지")
            parts.append(
                {
                    "inline_data": {
                        "mime_type": _guess_image_mime(reference_image_path),
                        "data": base64.b64encode(ref_bytes).decode("ascii"),
                    }
                }
            )
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        url = f"{_GEMINI_BASE}/models/{self.settings.gemini_image_model}:generateContent"

        last_exc: Exception | None = None
        for attempt in range(1 + self._MAX_FRAME_RETRIES):
            if attempt > 0:
                wait = 2.0 * attempt
                log.warning("nano_banana.frame.retry", attempt=attempt, wait=wait)
                time.sleep(wait)
            data = _send_json(
                lambda: self._client().post(
                    url, headers=_gemini_headers(self.settings), json=body
                ),
                "Gemini 프레임 생성",
            )
            try:
                image_bytes = self._extract_image_bytes(data)
            except VideoRenderError as exc:
                last_exc = exc
                continue
            out_path = Path(self.settings.nutti_media_dir) / f"frame_{uuid4().hex[:12]}.png"
            _write_bytes(out_path, image_bytes, "Gemini 프레임 이미지")
            log.info("nano_banana.frame.saved", path=str(out_path))
            return str(out_path)

        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _extract_image_bytes(data: dict) -> bytes:
        """generateContent 응답에서 첫 이미지 파트의 바이트를 방어적으로 추출한다.

        `candidates[0].content.parts[i].inline_data.data`를 직접 인덱싱하지 않고
        순회·isinstance 검사로 찾는다(실 API는 camelCase `inlineData`를 쓸 수
        있으므로 둘 다 허용). 이미지 파트가 없으면 무음 결함 대신 명시적으로
        실패하고, 본문 노출 없이 키 목록만 디버그 로그에 남긴다.
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
                    try:
                        return base64.b64decode(encoded)
                    except (binascii.Error, ValueError) as exc:
                        raise VideoRenderError(
                            f"Gemini 이미지 base64 디코드 실패: {type(exc).__name__}"
                        ) from None
        # finishReason이 있으면 더 명확한 오류 메시지 제공(SAFETY 필터 등 진단용).
        finish_reason = None
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict):
                finish_reason = first.get("finishReason")
        log.debug("nano_banana.missing_image", keys=list(data.keys()), finish_reason=finish_reason)
        if finish_reason:
            raise VideoRenderError(
                f"Gemini 이미지 파트 없음 — finishReason={finish_reason} "
                f"(응답 키: {list(data.keys())})"
            )
        raise VideoRenderError(
            f"Gemini 응답에 이미지 파트가 없습니다 (응답 키: {list(data.keys())})"
        )


class VeoClient(_HttpClosingMixin):
    """Veo 3.1 image-to-video 클라이언트(제출 → 폴링 → 즉시 다운로드).

    `POST /models/{model}:predictLongRunning`으로 작업을 제출하고, 반환된
    operation name을 `GET /{op_name}`으로 interval 간격·timeout 한도까지
    폴링한다. 완료되면 응답의 영상 URI에서 **즉시** 바이트를 내려받아
    `settings.nutti_media_dir`에 저장한다(48시간 삭제 정책 대비).

    오류 계약: HTTP·전송·JSON 파싱·디스크 쓰기 실패는 `VideoRenderError`,
    제한 시간 초과는 `VideoTimeoutError`. 폴링 중 일시 오류(429/5xx)는
    backoff와 함께 최대 `_MAX_TRANSIENT_RETRIES`회 재시도한다 — 작업은
    Google 쪽에서 계속 진행 중이므로 일시 오류 1회로 영구 포기하지 않는다.
    메시지에는 상태 코드/예외 타입명만 남긴다(operation id·URL·본문 금지).
    `poll_count`는 폴링 HTTP 시도 횟수(진단용)다.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        # 폴링/재시도 대기. 기본 time.sleep, 테스트에서 가짜 시계로 대체.
        self._sleep = sleep if sleep is not None else time.sleep
        self._interval = float(settings.veo_poll_interval_sec)
        # interval ≤ 0이면 elapsed가 영원히 0에 머물러 _poll이 무한 루프에 빠진다
        # (NUTTI_VEO_POLL_INTERVAL_SEC=0 같은 환경변수 오설정). 생성 시점에
        # 명확한 설정 오류로 빠르게 실패시킨다.
        if self._interval <= 0:
            raise ValueError(
                f"veo_poll_interval_sec는 0보다 커야 합니다(현재 {self._interval})"
            )
        self._timeout = float(settings.veo_timeout_sec)
        # timeout ≤ 0이면 _submit(과금 발생) 이후 while 조건이 첫 진입부터 False —
        # poll_count=0 VideoTimeoutError로 제출된 잡을 조용히 버리게 된다.
        # interval 가드와 대칭으로 생성 시점에 명확한 설정 오류로 빠르게 실패시킨다.
        if self._timeout <= 0:
            raise ValueError(
                f"veo_timeout_sec는 0보다 커야 합니다(현재 {self._timeout})"
            )
        # 진단용: 폴링 HTTP 시도 횟수(재시도 포함). 타임아웃 메시지에 포함된다.
        self.poll_count = 0

    def _client(self):
        """httpx 클라이언트를 지연 확보(주입 우선). dry_run에선 호출되지 않는다."""
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=60.0)
        return self._http

    def generate(self, frame_path: str, prompt: str) -> str:
        """시작 프레임 + 프롬프트로 영상을 생성하고 로컬 저장 경로를 반환한다."""
        op_name = self._submit(frame_path, prompt)
        uri = self._poll(op_name)
        return self._download(uri)

    def _submit(self, frame_path: str, prompt: str) -> str:
        """image-to-video 작업을 제출하고 operation name을 반환한다."""
        import base64

        frame_bytes = _read_bytes(frame_path, "Veo 시작 프레임")
        # TODO(live): 실제 Veo API 문서/응답으로 요청 바디 필드명
        # (instances.image.bytesBase64Encoded/mimeType, parameters.aspectRatio)을
        # 검증 필요 — 키 확보 후 확정한다(필드명 불일치 시 400).
        body = {
            "instances": [
                {
                    "prompt": prompt,
                    "image": {
                        "bytesBase64Encoded": base64.b64encode(frame_bytes).decode("ascii"),
                        "mimeType": _guess_image_mime(frame_path),
                    },
                }
            ],
            "parameters": {"aspectRatio": "9:16"},
        }
        url = f"{_GEMINI_BASE}/models/{self.settings.veo_model}:predictLongRunning"
        data = _send_json(
            lambda: self._client().post(
                url, headers=_gemini_headers(self.settings), json=body
            ),
            "Veo 작업 제출",
        )
        op_name = data.get("name")
        if not op_name:
            # 원본 본문에는 내부 식별자가 있을 수 있어 키 목록만 노출(redaction).
            log.debug("veo.submit.missing_name", keys=list(data.keys()))
            raise VideoRenderError(
                f"Veo 응답에 operation name이 없습니다 (응답 키: {list(data.keys())})"
            )
        # API 응답값은 신뢰 불가 입력 — 폴링 URL에 삽입하기 전에 형식을 검증한다.
        return _validate_op_name(str(op_name))

    def _poll(self, op_name: str) -> str:
        """operation을 완료까지 폴링해 영상 URI를 반환한다.

        경계는 `< timeout`로 둔다 — `<=`면 deadline에 도달한 뒤에도 1회 더
        폴링해(off-by-one) 제한 시간을 초과한 호출이 발생한다.
        """
        # op_name은 _submit의 _validate_op_name에서 이미 검증·정규화됐다
        # (선행 슬래시 제거·허용 문자·operations 세그먼트 확인). 일부 Google LRO
        # API는 선행 슬래시 포함('/v1beta/…') 형태를 반환하지만 정규화로 이중
        # 슬래시 404를 막는다. TODO(live): 실 Veo 응답의 name 형식(상대
        # `operations/…` vs 절대 `/v1beta/…`)을 키 확보 후 확정한다.
        url = f"{_GEMINI_BASE}/{op_name}"
        elapsed = 0.0
        while elapsed < self._timeout:
            data, backoff_sec = self._poll_once(url)
            # 일시 오류 재시도의 backoff 대기도 wall-clock 한도에 누적한다 —
            # 누적하지 않으면 매 폴링이 재시도 한도까지 포화할 때(폴링당 +14s)
            # 실제 대기가 설정 timeout의 약 2배(기본값 기준 ~1160s)에 이른다.
            elapsed += backoff_sec
            if data.get("done"):
                error = data.get("error")
                if error:
                    # error.message는 내부 상세가 박힐 수 있어 code만 노출(redaction).
                    code = error.get("code") if isinstance(error, dict) else error
                    raise VideoRenderError(f"Veo 작업 실패: error code {code}")
                return self._extract_video_uri(data)
            # 아직 진행 중 → 대기 후 재시도. 경과 시간은 sleep 누적으로 추적
            # (가짜 시계 주입 시에도 결정적).
            self._sleep(self._interval)
            elapsed += self._interval
        # operation id는 메시지에 노출하지 않는다(redaction) — 폴링 횟수로 진단.
        raise VideoTimeoutError(
            f"Veo 폴링 타임아웃({self._timeout:.0f}s, 폴링 {self.poll_count}회)"
        )

    def _poll_once(self, url: str) -> tuple[dict, float]:
        """상태 1회 조회. 일시 오류(429/5xx)는 지수 backoff로 최대 3회 재시도한다.

        영구 오류(그 외 4xx)·전송 오류·JSON 파싱 실패는 즉시 VideoRenderError로
        전파한다. 반환은 `(응답 dict, 재시도 backoff 합계 초)` — backoff 대기
        (2·4·8초, 폴링 1회분당 최대 14초)는 호출부(_poll)가 timeout 경과에
        누적한다. 누적하지 않으면 재시도 포화 시 wall-clock이 설정 한도를
        크게 초과한다(기본 600s 설정에서 최악 ~93% 오버런).
        """
        attempts = 0
        backoff_total = 0.0
        while True:
            self.poll_count += 1
            resp = _safe_send(
                lambda: self._client().get(url, headers=_gemini_headers(self.settings)),
                "Veo 상태 조회",
            )
            code = getattr(resp, "status_code", None)
            transient = isinstance(code, int) and (code == 429 or code >= 500)
            if transient and attempts < _MAX_TRANSIENT_RETRIES:
                attempts += 1
                # 지수 backoff(2·4·8초). 가짜 sleep 주입 시 즉시 반환된다.
                wait = _RETRY_BACKOFF_SEC * (2 ** (attempts - 1))
                self._sleep(wait)
                backoff_total += wait
                continue
            return _json_or_raise(resp, "Veo 상태 조회"), backoff_total

    @staticmethod
    def _extract_video_uri(data: dict) -> str:
        """완료 응답에서 영상 URI를 방어적으로 추출한다(직접 인덱싱 금지)."""
        response = data.get("response")
        gvr = response.get("generateVideoResponse") if isinstance(response, dict) else None
        samples = gvr.get("generatedSamples") if isinstance(gvr, dict) else None
        first = samples[0] if isinstance(samples, list) and samples else None
        video = first.get("video") if isinstance(first, dict) else None
        uri = video.get("uri") if isinstance(video, dict) else None
        if not uri:
            # 원본 본문에는 서명 URL·내부 메타데이터가 있을 수 있어 키 목록만 노출.
            log.debug("veo.poll.missing_uri", keys=list(data.keys()))
            raise VideoRenderError(
                f"Veo 완료 응답에 영상 URI가 없습니다 (응답 키: {list(data.keys())})"
            )
        return str(uri)

    def _download(self, uri: str) -> str:
        """완료된 영상을 즉시 내려받아 media_dir에 저장하고 경로를 반환한다.

        `x-goog-api-key`는 자격증명이므로 **Gemini API 도메인일 때만** 붙인다.
        Gemini 파일 API는 실제 바이트를 GCS 서명 URL로 302 리다이렉트할 수 있다.
        GCS 서명 URL은 쿼리파라미터로 자체 인증하므로 API 키 헤더 없이 재요청한다.
        """
        # Veo 완료 응답의 URI도 API 응답값(신뢰 불가 입력) — scheme·host 검증 필수.
        _validate_redirect_location(uri)
        headers = _gemini_headers(self.settings) if uri.startswith(_GEMINI_HOST) else None
        # follow_redirects=False: 리다이렉트 대상이 GCS 등 외부 호스트일 때
        # API 키 헤더가 새지 않도록 수동으로 처리한다.
        resp = _safe_send(
            lambda: self._client().get(uri, headers=headers, follow_redirects=False),
            "Veo 영상 다운로드",
        )
        sc = getattr(resp, "status_code", None)
        if not isinstance(sc, int):
            raise VideoRenderError("Veo 영상 다운로드 응답에 유효한 status_code가 없습니다")
        if 300 <= sc < 400:
            location = (getattr(resp, "headers", {}) or {}).get("location", "")
            if not location:
                raise VideoRenderError("Veo 영상 다운로드: 리다이렉트 응답에 Location 헤더 없음")
            # SSRF 방어: Location 헤더(API 응답값)는 신뢰 불가 — scheme·host 검증 필수.
            _validate_redirect_location(location)
            # Gemini 도메인 리다이렉트는 API 키 헤더를 유지해야 한다.
            # GCS 서명 URL은 API 키 없이 쿼리파라미터로 자체 인증한다.
            # follow_redirects=False: 검증된 Location 이후의 추가 hop을 차단(SSRF 체인 방지).
            redir_headers = _gemini_headers(self.settings) if location.startswith(_GEMINI_HOST) else None
            resp = _safe_send(
                lambda: self._client().get(location, headers=redir_headers, follow_redirects=False),
                "Veo 영상 다운로드(리다이렉트)",
            )
            r_sc = getattr(resp, "status_code", None)
            if isinstance(r_sc, int) and 300 <= r_sc < 400:
                raise VideoRenderError("Veo 다운로드: 허용 호스트 이후 추가 리다이렉트 금지")
        _raise_for_status(resp, "Veo 영상 다운로드")
        content = getattr(resp, "content", None)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise VideoRenderError("Veo 다운로드 응답에 영상 바이트가 없습니다")
        out_path = Path(self.settings.nutti_media_dir) / f"video_{uuid4().hex[:12]}.mp4"
        # 디스크 쓰기 실패(OSError)도 VideoRenderError 계약을 지킨다(_write_bytes).
        _write_bytes(out_path, bytes(content), "Veo 영상")
        log.info("veo.video.saved", path=str(out_path))
        return str(out_path)


class VideoStudio:
    """대본 → 시작 프레임(NanoBanana) → Veo 영상 생성을 담당하는 파사드(facade)."""

    def __init__(
        self,
        settings: Settings,
        *,
        nano_client=None,
        veo_client=None,
        sleep=None,
    ):
        # 실연동 클라이언트는 주입 가능하게 받는다(테스트에서 fake 주입 → 네트워크 불요).
        # 주입이 없으면 각 실 경로(non-dry_run)에서 지연 생성한다.
        self.settings = settings
        self._nano_client = nano_client
        self._veo_client = veo_client
        # 폴링 대기용 sleep 주입(기본 time.sleep). 테스트에서 가짜 시계로 대체.
        self._sleep = sleep

    def validate_config(self) -> None:
        """실 경로 진입 전 필수 API 키가 쓸 수 있는 값인지 한 번에 점검한다.

        dry_run이면 키가 없어도 되므로 즉시 통과한다. 실 경로(dry_run=False)에서
        키가 비어 있으면, 인증 401을 받고 나서야 불투명한 'HTTP 401'로 실패하는
        대신 시작 시점에 명확한 설정 오류(ValueError)로 빠르게 실패한다.
        클라이언트가 모두 주입됐으면 키 검사를 건너뛴다(테스트/대체 구현 허용).
        키 판정은 `_usable_key`로 한다 — `.env`의 `GEMINI_API_KEY=  # 설명`처럼
        인라인 주석이 값으로 파싱되는 패턴을 진짜 키로 오인하지 않기 위함이다.
        """
        if self.settings.dry_run:
            return
        needs_key = self._nano_client is None or self._veo_client is None
        if needs_key and not _usable_key(self.settings.gemini_api_key):
            raise ValueError("GEMINI_API_KEY가 비어 있습니다 — dry_run=False 시 필수입니다.")

    def produce(self, script: Script) -> VideoAsset:
        """시작 프레임 생성 → Veo 프롬프트 → 영상 생성 → VideoAsset 반환."""
        # 실 경로면 시작 전에 필수 키를 검증(미설정 시 빠르게 실패).
        self.validate_config()

        if self.settings.dry_run:
            log.info("dry_run.video", script_id=script.id)
            frame_path = f"data/dry_run/frame_{script.id}.jpg"
            video_path = f"data/dry_run/video_{script.id}.mp4"
            return VideoAsset(
                script_id=script.id,
                frame_image_path=frame_path,
                video_path=video_path,
                final_url=video_path,
                duration_sec=8.0,
            )

        frame_path = self._generate_frame(script)
        prompt = VeoPromptBuilder().build(script)
        video_path = self._generate_video(frame_path, prompt)
        return VideoAsset(
            script_id=script.id,
            frame_image_path=frame_path,
            video_path=video_path,
            final_url=video_path,
            duration_sec=8.0,
        )

    def _generate_frame(self, script: Script) -> str:
        """NanoBanana로 시작 프레임을 생성한다(마스코트 레퍼런스 이미지 선택 첨부).

        주입된 클라이언트는 소유자가 닫고, 여기서 만든 것만 finally에서 닫는다
        (httpx 연결 풀 누수 방지). 주입분은 owned=None으로 둬 close하지 않는다.
        """
        client = self._nano_client
        owned = None
        if client is None:
            client = owned = NanoBananaClient(self.settings)
        try:
            path = client.generate_frame(
                self._frame_prompt(script),
                reference_image_path=self.settings.nutti_mascot_image or None,
            )
        finally:
            if owned is not None:
                _close_owned(owned)
        log.info("video.frame.done", script_id=script.id)
        return path

    def _generate_video(self, frame_path: str, prompt: str) -> str:
        """Veo로 영상을 생성한다(제출 → 폴링 → 즉시 다운로드 → 로컬 경로).

        자체 생성분만 finally에서 닫는다(실패 시에도 정확히 1회 close).
        """
        client = self._veo_client
        owned = None
        if client is None:
            client = owned = VeoClient(self.settings, sleep=self._sleep)
        try:
            path = client.generate(frame_path, prompt)
        finally:
            if owned is not None:
                _close_owned(owned)
        log.info("video.veo.done", path=path)
        return path

    @staticmethod
    def _frame_prompt(script: Script) -> str:
        """시작 프레임 생성용 장면 프롬프트(마스코트·세로 9:16·금지 요소 명시).

        주제도 AI 생성 텍스트이므로 `_sanitize_prompt_text`로 정제해 삽입한다
        (작은따옴표 치환 + 길이 제한 — 간접 프롬프트 주입 심층 방어).
        """
        topic = _sanitize_prompt_text(script.topic, _MAX_TOPIC_CHARS)
        return (
            "A photorealistic vertical 9:16 starting frame for a short-form video: "
            "the Nutti dog mascot sitting in a cozy, warmly lit studio, "
            f"looking at the camera. Topic: {topic}. "
            "No people, no additional animals, no on-screen text."
        )
