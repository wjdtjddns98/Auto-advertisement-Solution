"""영상 생성 연동: FLUX Kontext(fal.ai 시작 프레임) → Veo 3.1 image-to-video 단일컷 8초.

흐름: ① FalKontextClient(FLUX.1 Kontext pro)가 마스코트 레퍼런스 이미지를 편집해
시작 프레임 이미지를 생성해 로컬 저장 →
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
import zlib
from pathlib import Path
from typing import NamedTuple
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
# 비트 1개(8초/7초)의 대사는 짧으므로 이 한도를 넘을 이유가 없다.
_MAX_DIALOGUE_CHARS = 500
_MAX_TOPIC_CHARS = 200
# 영상 길이 구성(veo 경로): 첫 비트는 image-to-video 8초 클립, 이후 비트는 Veo 영상
# 연장(extend)으로 직전 클립을 이어 받아 +7초씩 늘린다 — 점프컷 없는 단일 연속 영상.
# 9:16 세로 extend는 요청에 aspectRatio="9:16"을 명시해야만 동작한다 — 생략하면 Gemini
# API가 출력을 16:9로 가정해 9:16 입력을 400으로 거부한다(2026-06-15 유료 실측; 공식 문서는
# 지원으로 표기하나 실 API는 이 파라미터 없이는 막힘). 단 extend는 Fast/Standard 모델만 —
# Lite 미지원. 비트 N개 → 8+7*(N-1)초, 기본 4비트 = 29초. kling 경로는 별도(ffmpeg 스티칭).
_CLIP_SEC = 8.0
# Veo extend 1회가 추가하는 길이(초). durationSeconds=8로 요청해도 출력 추가분은 7초.
_EXTEND_SEC = 7.0
# Veo가 화면에 텍스트(특히 깨진 한글 자막)를 임의 렌더하는 것을 억제하는 negativePrompt.
# 대사를 음성으로만 내보내기 위함 — 실측에서 이 파라미터로 자막이 사라짐을 확인.
_VEO_NEGATIVE_PROMPT = (
    "text, subtitles, captions, words, letters, writing, watermark, "
    "on-screen text, caption bar, hardcoded subtitles, korean text overlay"
)
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


def _send_json(
    send,
    what: str,
    *,
    sleep=None,
    max_transient_retries: int = 0,
    retry_400: bool = False,
) -> dict:
    """전송 + 상태 검증 + JSON 파싱을 한 번에 — 어떤 실패든 VideoRenderError로.

    `max_transient_retries > 0`이면 일시 오류(HTTP 429 또는 5xx)에 한해 지수
    backoff(2·4·8초)로 그 횟수만큼 재시도한다 — 무료 티어 Gemini는 분당 한도
    (RPM)가 낮아 풀 파이프라인이 단계를 연달아 호출하면 일시적 429가 흔히 난다.
    폴링(_poll_once)과 동일한 분류(429 또는 5xx)·backoff를 쓴다. 기본값 0이면
    재시도 없이 기존 동작(즉시 전파)을 유지한다(Kling submit/result).
    영구 오류(그 외 4xx)·전송/JSON 파싱 실패는 재시도 없이 즉시 전파한다.

    `retry_400=True`면 HTTP 400도 일시 오류로 분류해 재시도한다 — Veo 제출
    (predictLongRunning)은 동일한 요청이 400과 200을 비결정적으로 오가는 간헐
    400이 실측 확인됐다(2026-06-15 유료 실측: 동일 body가 한 호출은 400, 직후
    재시도는 200 + operation name 발급). 영구 400(잘못된 입력)도 함께 재시도되나
    제출 400은 영상 생성 이전이라 과금이 없고 backoff 몇 초만 손해이므로,
    간헐 400으로 파이프라인 전체가 죽는 것을 막는 편이 이득이다.
    `sleep`은 테스트가 가짜 시계를 주입하기 위한 훅(기본 time.sleep)이다.
    """
    _sleep = sleep if sleep is not None else time.sleep
    attempts = 0
    while True:
        resp = _safe_send(send, what)
        code = getattr(resp, "status_code", None)
        transient = isinstance(code, int) and (
            code == 429 or code >= 500 or (retry_400 and code == 400)
        )
        if transient and attempts < max_transient_retries:
            attempts += 1
            # 지수 backoff(2·4·8초). 가짜 sleep 주입 시 즉시 반환된다.
            _sleep(_RETRY_BACKOFF_SEC * (2 ** (attempts - 1)))
            continue
        return _json_or_raise(resp, what)


def _write_bytes(out_path: Path, data: bytes, what: str) -> None:
    """바이트를 디스크에 원자적으로 저장한다. OSError(디스크 풀/권한)도 VideoRenderError로.

    tmp 파일에 먼저 쓴 뒤 os.replace로 교체한다(JsonFileReviewStore와 동일 패턴) —
    쓰기 도중 크래시(SIGKILL·전원·디스크 풀)가 나도 truncated 영상/프레임이
    media_dir에 남아 누적되지 않는다(영상은 수백 MB라 디스크 누수 위험).
    호출부(오케스트레이터)는 영상 서브시스템에서 VideoRenderError만 기대하므로,
    쓰기 실패를 그대로 새지 않게 한다. 메시지는 예외 타입명만(경로 노출 금지).
    """
    import os

    tmp_path = out_path.with_name(out_path.name + ".tmp")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(data)
        os.replace(tmp_path, out_path)
    except OSError as exc:
        # os.replace 실패(Windows: 대상이 다른 프로세스에 열려 있으면 PermissionError) 시
        # tmp 잔재(수백 MB 영상)가 디스크에 남지 않도록 정리한 뒤 전파한다.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
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


class EpisodeStyle(NamedTuple):
    """편 단위 연출 스타일(의상·장소상황).

    한 편 안에서는 시작 프레임과 모든 비트 프롬프트가 같은 스타일을 공유해
    시각 일관성을 유지하고, 편이 바뀌면 다른 조합이 나와 채널이 단조롭지 않게 한다.
    """

    outfit: str
    setting: str


# ======================= PO 수정 구역 (편별 연출 로테이션) =======================
# 편마다 마스코트의 "옷"과 "장소·상황"이 바뀐다(2026-06-12 PO 지시 — 매번 다른 옷,
# 다른 장소·상황에서 인터뷰하는 느낌). 항목을 추가/삭제하면 조합 수가 바뀐다
# (현재 6×6=36 조합). 영어 묘사에 ASCII 작은따옴표(')는 금지 — 비트 프롬프트의
# 대사 인용 구분자와 충돌해 주입 방어 검증이 깨진다(U+2019는 허용).
_EPISODE_OUTFITS = [
    "a tiny yellow raincoat",
    "a cozy cream knitted sweater",
    "a crisp little navy suit with a red bow tie",
    "a sporty grey hoodie",
    "a light blue denim jacket",
    "a fluffy red scarf with a matching beanie",
]
_EPISODE_SETTINGS = [
    "standing on a busy city sidewalk like a street interview",
    "sitting on a cozy living room sofa under warm lamps",
    "sitting on a park bench on a sunny afternoon",
    "standing at a bright modern kitchen counter",
    "standing in front of a cute pet shop entrance",
    "sitting at a tidy home office desk like a news anchor",
]
# ===================== PO 수정 구역 끝 (편별 연출 로테이션) =====================


def pick_episode_style(script_id: str) -> EpisodeStyle:
    """script.id의 CRC32로 의상·장소상황을 결정적으로 고른다(같은 편=같은 스타일).

    의상과 장소는 서로 다른 salt로 해시해 독립적으로 조합된다 — 같은 salt를 쓰면
    리스트 길이가 같을 때 인덱스가 동기화돼 조합 다양성이 리스트 길이로 줄어든다.
    Supertone 보이스 로테이션(tts_supertone)과 같은 결정적 선택 패턴.
    """
    outfit_idx = zlib.crc32(f"outfit:{script_id}".encode()) % len(_EPISODE_OUTFITS)
    setting_idx = zlib.crc32(f"setting:{script_id}".encode()) % len(_EPISODE_SETTINGS)
    return EpisodeStyle(_EPISODE_OUTFITS[outfit_idx], _EPISODE_SETTINGS[setting_idx])


def _veo_total_sec(n_beats: int) -> float:
    """veo extend 경로의 총 길이초: 첫 비트 8초 + 이후 비트마다 +7초(extend).

    비트가 0~1개면 extend가 없어 8초(또는 그 이하 1개)다. 음수 방지로 max(0, …).
    """
    return _CLIP_SEC + _EXTEND_SEC * max(0, n_beats - 1)


# ============== PO 수정 구역 (마스코트 외형 — 캐릭터 일관성의 핵심) ==============
# 마스코트 "강아지 자체"의 고정 외형(캐릭터 시트). 시작 프레임과 모든 비트 프롬프트에
# 똑같이 박아, 편이 바뀌고 옷·장소가 달라져도 "같은 강아지"로 보이게 한다.
# 텍스트로 외형을 고정하는 것이 일관성의 핵심 수단 — 비워 두면 생성기가 매 편 다른
# 강아지를 지어낸다(실제 증상). 반드시 레퍼런스 이미지(assets/mascot.png,
# NUTTI_MASCOT_IMAGE)의 실제 모습과 일치시킬 것 — 텍스트와 이미지가 어긋나면 둘을
# 섞어 오히려 더 들쭉날쭉해진다. 현재 값은 assets/mascot.png(흰 비숑프리제, PO 제공
# 마스코트.png 1254x1254) 기준의 "얌전하고 귀여운 puppy". ASCII 작은따옴표(') 금지(대사 인용 구분자와 충돌).
_MASCOT_APPEARANCE = (
    "a real, photorealistic, live small white Bichon Frise puppy with a soft, fluffy, "
    "rounded pure-white powder-puff coat groomed into a round teddy-bear face, round dark "
    "eyes, a small black nose, and a normal four-legged small dog body — a real live "
    "animal, never a person in an animal costume, never a mascot suit or fursuit, never a "
    "plush toy or stuffed animal"
)
# ==================== PO 수정 구역 끝 (마스코트 외형) ====================


# ============== PO 수정 구역 (시네마틱 화질·조명 — 톤 일관성) ==============
# 모든 클립과 시작 프레임에 동일하게 박는 고정 "화질·조명·심도" 묘사. 한 번 정해
# 일관되게 적용해 편마다 영상 톤이 들쭉날쭉하지 않게 하고, 밋밋한 핸드폰 영상 느낌을
# 줄여 완성도를 높인다. 단, 장면(의상·장소)·카메라 무빙·캐릭터 외형은 여기서 건드리지
# 말 것 — extend 연속성·구도 안정성·캐릭터 일관성과 충돌한다. extend(이어붙이는 클립)
# 에는 넣지 않는다(첫 클립+시작 프레임이 룩을 정하면 연장 구간이 시각적으로 계승).
# ASCII 작은따옴표(') 금지(대사 인용 구분자와 충돌).
_CINEMATIC_LOOK = (
    "Cinematic look: soft natural daylight with a gentle warm key light, shallow depth "
    "of field with a softly blurred background, crisp sharp focus on the puppy, "
    "photorealistic fine fur detail, clean high-resolution clarity."
)
# ==================== PO 수정 구역 끝 (시네마틱 화질·조명) ====================


class VeoPromptBuilder:
    """Veo 3.1 image-to-video 프롬프트 빌더(비트별 클립·네이티브 한국어 음성).

    규칙(연구 노트 + 2026-06-12 실테스트 PO 피드백 기반):
    - 대사는 작은따옴표로 인용해 Veo 네이티브 음성으로 발화시킨다(별도 TTS 불요).
    - 페르소나·목소리 묘사를 모든 비트에 동일하게 박는다 — 클립이 독립 생성되므로
      목소리 일관성은 프롬프트가 유일한 통제 수단(실테스트에서 비트마다 목소리가
      달라지는 문제 확인 → 상세 고정 묘사로 드리프트 완화).
    - 인터뷰 마이크를 화면 밖에서 들이대는 길거리 인터뷰 구도(참고: "오줌싸개 강아지의
      억울한 변명"·"조회수 두자리 강아지의 한마디" 류 쇼츠).
    - 카메라는 고정(locked-off tripod)·무빙 없음 — 흔들림/컷 전환 방지.
    - 깨짐 주원인(추가 동물·사람·화면 내 텍스트)을 명시적으로 금지한다.
    - 포맷: photorealistic · 9:16 세로 · 각 비트는 8초 단일컷(여러 비트는 ffmpeg로 스티칭).
    """

    # =========================== PO 수정 구역 (영상 연출) ===========================
    # 영상의 "연기·카메라·말투"를 바꾸려면 아래 영어 템플릿을 고친다.
    # · _PERSONA: 마스코트 캐릭터(얌전하고 귀여운 puppy·차분한 인터뷰 톤). 외형은
    #   _MASCOT_APPEARANCE로 고정되고, 여기선 성격·태도만 정한다. 과장 표정 단어
    #   (cheeky/exaggerated 등)를 넣으면 얼굴이 일그러지므로(괴랄) 피한다
    # · _VOICE: 목소리 고정 묘사 — 비트 간 목소리 일관성의 핵심. 모든 클립에 동일하게
    #   들어가야 하므로 함부로 빼지 말 것. 목소리 톤을 바꾸려면 묘사 내용만 교체
    # · _MIC: 화면 밖 인터뷰 마이크 연출(사람은 화면에 안 나옴)
    # · _SPEAKING_OFF / _SPEAKING_DIRECT: 마스코트가 누구에게 말하는지(인터뷰 톤 vs 정면)
    # · _CAMERA: 카메라 워크(고정·클로즈업). 흔들면 립싱크/일관성 깨짐 위험 ↑
    # · _NEGATIVE: 금지 요소(사람·다른 동물·화면 자막/글자). Veo가 깨진 한글 자막을
    #   임의로 박는 걸 막는 핵심 방어 — 함부로 빼지 말 것(_VEO_NEGATIVE_PROMPT와 이중 방어)
    # 모든 템플릿에 ASCII 작은따옴표(') 금지 — 대사 인용 구분자와 충돌(주입 방어 깨짐).
    # 한국어로 "이렇게 바꾸고 싶다"만 정해도 됨 — 영어 반영은 개발자에게 요청 권장.
    # 주의: 브랜드명("Nutti")·치수("9:16") 같은 리터럴을 넣지 말 것 — Veo가 그 글자를
    # 화면 자막으로 렌더한다(실측: "Nutti"·"9:16" 자막 박힘). "mascot"도 금지 — 인형탈
    # 코스튬으로 해석된다(실측). 캐릭터는 항상 "진짜 실사 강아지"로 못박는다.
    _PERSONA = (
        f"{_MASCOT_APPEARANCE}, calm and gentle, talking to the camera in a relaxed, "
        "friendly way, with soft, natural, subtle facial expressions and no "
        "exaggerated or distorted faces"
    )
    _VOICE = (
        "Voice (must be EXACTLY the same voice in every clip of this series): a bright, "
        "cute Little girl Korean voice, slightly high-pitched, cheeky and energetic, "
        "speaking at a lively natural pace. Keep the identical timbre, pitch, and accent "
        "in every clip; do not change the voice or switch to a different speaker for "
        "emphasis or for the final call-to-action line."
    )
    _MIC = (
        "A handheld interview microphone is pointed at the puppy from off-screen; "
        "the person holding it stays completely out of frame."
    )
    _SPEAKING_OFF = "speaking in Korean to an off-screen interviewer"
    _SPEAKING_DIRECT = "speaking in Korean directly to the camera"
    _CAMERA = "Camera: locked-off tripod shot, no camera movement."
    _NEGATIVE = (
        "The subject is a real live photorealistic puppy — never a mascot suit, fursuit, "
        "costume, person in a costume, or plush toy. Strictly no additional animals, no "
        "people. Absolutely no text, subtitles, captions, letters, numbers, words, logos, "
        "brand names, watermarks, or UI overlays anywhere in the frame."
    )
    # ========================= PO 수정 구역 끝 (영상 연출) =========================

    def build(
        self,
        script: Script,
        *,
        off_screen_interviewer: bool = True,
        style: EpisodeStyle | None = None,
    ) -> str:
        """대본에서 단일컷 Veo 프롬프트를 만든다(하위호환·단일 비트 폴백).

        본문이 비면 주제로 폴백(빈 인용 방지). 멀티비트 경로는 `build_beat`를 쓴다.
        """
        text = script.body.strip() or script.topic
        return self.build_beat(text, off_screen_interviewer=off_screen_interviewer, style=style)

    def build_beat(
        self,
        dialogue_text: str,
        *,
        off_screen_interviewer: bool = True,
        style: EpisodeStyle | None = None,
    ) -> str:
        """비트 대사 한 토막으로 8초 단일컷 Veo 프롬프트를 만든다.

        페르소나·고정 목소리 묘사는 항상 포함되고, `style`이 주어지면 의상·장소상황
        문장이 추가된다(편 안에서 모든 비트가 같은 style을 받아 장면 연속성 유지).
        인터뷰 마이크 연출(_MIC)은 off_screen_interviewer=True일 때만 붙는다 —
        정면 발화 모드는 마이크 없는 1인 방송 톤.
        대사는 음성(spoken audio only)으로만 발화시키고 화면 자막을 금지한다 — Veo가
        한글 자막을 임의 렌더하면 깨진 글자로 나오기 때문(negativePrompt와 이중 방어).
        대사는 `_sanitize_prompt_text`로 정제한다 — 작은따옴표가 있으면 인용 구분자를
        탈출해 금지 지시(추가 동물·사람·텍스트 금지)를 덮어쓰는 주입이 가능하기 때문이다.
        """
        dialogue = _sanitize_prompt_text(dialogue_text.strip() or "", _MAX_DIALOGUE_CHARS)
        speaking = self._SPEAKING_OFF if off_screen_interviewer else self._SPEAKING_DIRECT
        scene = ""
        if style is not None:
            scene = f"The puppy wears {style.outfit}, {style.setting}. "
        mic = f"{self._MIC} " if off_screen_interviewer else ""
        return (
            f"A photorealistic shot of {self._PERSONA}, {speaking}, "
            f"saying (as spoken audio only, no on-screen text): '{dialogue}'. "
            f"{scene}{mic}"
            f"{self._VOICE} "
            f"{self._CAMERA} "
            f"{_CINEMATIC_LOOK} "
            "Format: tall vertical portrait orientation, single continuous 8-second shot. "
            f"{self._NEGATIVE}"
        )

    def build_extend_beat(
        self,
        dialogue_text: str,
        *,
        off_screen_interviewer: bool = True,
    ) -> str:
        """직전 클립을 이어받는 Veo extend용 프롬프트를 만든다(연속 동작·발화만 묘사).

        배경·의상·시작 장면은 직전 세그먼트에서 자동 계승되므로 재명시하지 않는다 —
        과도한 장면 재설정 지시는 시각적 불연속을 유발할 수 있다(공식 extend 가이드).
        목소리 고정 묘사(_VOICE)·카메라(_CAMERA)·금지 요소(_NEGATIVE)는 비트 간 드리프트를
        막기 위해 유지한다. 마스코트가 클립 끝까지 끊김 없이 말하도록 유도해 직전 클립
        마지막 1초에 음성이 남게 한다 — 마지막 1초에 음성이 없으면 연장 구간에서 발화가
        이어지지 않기 때문(공식 가이드: voice not extended if not present in last 1 second).
        대사는 build_beat과 동일하게 `_sanitize_prompt_text`로 정제해 인용 구분자 주입을 막는다.
        """
        dialogue = _sanitize_prompt_text(dialogue_text.strip() or "", _MAX_DIALOGUE_CHARS)
        speaking = self._SPEAKING_OFF if off_screen_interviewer else self._SPEAKING_DIRECT
        return (
            "Continue the same uninterrupted shot with no cut and no scene change. "
            f"The same {self._PERSONA} keeps {speaking}, now saying "
            f"(as spoken audio only, no on-screen text): '{dialogue}'. "
            "The mascot keeps talking continuously to the very end with no silent pause. "
            f"{self._VOICE} "
            f"{self._CAMERA} "
            f"{self._NEGATIVE}"
        )


# NanoBananaClient(Gemini 이미지 생성)는 2026-06 PO 결정으로 FalKontextClient로 교체됨.
# 프레임 클라이언트는 nutti/integrations/image_kontext.py의 FalKontextClient를 사용한다.


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
        """시작 프레임 + 프롬프트로 8초 영상을 생성하고 로컬 저장 경로를 반환한다.

        단일 비트(연장 없음)와 외부 호출용 경로다. 다중 비트 연속 영상은
        `_generate_uri`로 첫 클립 URI만 얻어 extend로 체이닝한 뒤 마지막에 1회만
        다운로드한다(중간 클립 다운로드 불요). 해상도는 720p로 고정한다 — Veo extend
        출력이 720p 고정이라, 첫 클립도 720p로 맞춰 첫 연장 경계에서 해상도 점프가
        생기지 않게 한다.
        """
        return self._download(self._generate_uri(frame_path, prompt))

    def _generate_uri(self, frame_path: str, prompt: str) -> str:
        """첫 클립을 생성하고 Files API 영상 URI를 반환한다(다운로드 없음).

        extend 입력은 직전 클립의 **URI 참조**를 요구하므로(인라인 base64 거부, 실측),
        다중 비트 체이닝에서는 중간 클립을 내려받지 않고 이 URI를 다음 extend로 넘긴다.
        """
        op_name = self._submit(frame_path, prompt)
        return self._poll(op_name)

    def extend(self, prev_video_uri: str, prompt: str) -> str:
        """직전 클립 URI를 이어 +7초 연장하고, 누적 영상의 새 Files API URI를 반환한다.

        extend 입력은 Files API URI 참조(`instances[].video.uri`)만 받는다 — 인라인
        base64는 400 "Video URI not found", gcsUri·inlineData는 모델 미지원(2026-06-15
        실측 확정). 직전 `_generate_uri`/`extend`가 받은 URI를 그대로 넘기므로 중간
        클립을 재다운로드·재업로드하지 않는다. 각 extend 호출은 입력 영상에 새 구간을
        덧붙인 **누적 전체 영상**의 URI를 돌려주므로, 마지막 호출 결과가 곧 최종 연속
        영상이고 호출부가 그것만 1회 다운로드한다(별도 스티칭 불요). 9:16 세로 extend는
        Fast/Standard 모델만 지원한다(Lite 불가 — 호출부에서 사전 차단). 직전 클립의
        마지막 1초에 음성이 없으면 연장 구간 음성이 이어지지 않으므로(공식 가이드),
        extend 프롬프트(VeoPromptBuilder.build_extend_beat)는 끊김 없는 발화를 유도한다.
        """
        op_name = self._submit_extend(prev_video_uri, prompt)
        return self._poll(op_name)

    def _submit(self, frame_path: str, prompt: str) -> str:
        """image-to-video 작업을 제출하고 operation name을 반환한다.

        `negativePrompt`로 화면 텍스트를 억제한다 — Veo가 대사를 화면 자막으로 임의
        렌더하면 한글 자형을 못 그려 깨진 글자로 나오기 때문(실측 확인된 약점).
        """
        import base64

        frame_bytes = _read_bytes(frame_path, "Veo 시작 프레임")
        # resolution 720p: extend 출력이 720p 고정이라 첫 클립도 720p로 맞춘다(경계 점프 방지).
        params: dict = {"aspectRatio": "9:16", "resolution": "720p"}
        # Veo 3.1 Lite는 negativePrompt를 보내면 HTTP 400으로 거부한다
        # ("`negativePrompt` isn't supported by this model", 2026-06-12 실측).
        # Lite에서는 프롬프트 본문의 자막 금지 문구(_NEGATIVE·'no on-screen text')가
        # 1차 방어로 남는다 — Lite probe 실측에서 자막 없음 확인(PO 판정 ③).
        if self._supports_negative_prompt():
            params["negativePrompt"] = _VEO_NEGATIVE_PROMPT
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
            "parameters": params,
        }
        return self._submit_body(body)

    def _submit_extend(self, prev_video_uri: str, prompt: str) -> str:
        """직전 클립 URI를 입력으로 영상 연장(extend) 작업을 제출하고 operation name을 반환한다.

        image-to-video(`_submit`)와 같은 predictLongRunning 엔드포인트를 쓰되, instance
        입력이 image가 아니라 직전 클립 `video.uri`(Files API URI 참조)다. 인라인 base64는
        거부되고(400 "Video URI not found in the request."), `gcsUri`·`inlineData`는 모델
        미지원이라 거부된다(전부 2026-06-15 실측 확정). 입력 URI는 직전 generate/extend
        완료 응답의 `generatedSamples[0].video.uri`를 그대로 쓴다(재다운로드·재업로드 불요).
        출력은 720p 고정. aspectRatio="9:16"을 반드시 명시한다 — 생략하면 extend가 출력을
        16:9로 가정해 9:16 입력을 HTTP 400("Aspect ratio of the input video must be 16:9,
        but got: 9:16")으로 거부한다(2026-06-15 유료 실측 확정). 명시하면 9:16 입력을 받아
        720x1280 연속 영상을 낸다. 공식 문서는 9:16 extend 지원으로 표기하나 실제 Gemini
        API는 이 파라미터 없이는 막힌다(docs/API 불일치, 미수정).
        """
        params: dict = {"aspectRatio": "9:16", "resolution": "720p"}
        # negativePrompt: 자막 억제. Fast/Standard만 지원(Lite는 400) — 모델명으로 분기.
        if self._supports_negative_prompt():
            params["negativePrompt"] = _VEO_NEGATIVE_PROMPT
        body = {
            "instances": [{"prompt": prompt, "video": {"uri": prev_video_uri}}],
            "parameters": params,
        }
        return self._submit_body(body)

    def _supports_negative_prompt(self) -> bool:
        """현재 설정 모델이 negativePrompt 파라미터를 받는지 여부(모델명 기반).

        veo-3.1-lite-generate-preview는 미지원(400 거부, 2026-06-12 실측)이므로
        모델명에 'lite'가 포함되면 미지원으로 본다. standard/fast는 지원.
        """
        return "lite" not in self.settings.veo_model.lower()

    def _submit_body(self, body: dict) -> str:
        """predictLongRunning 본문을 제출하고 검증된 operation name을 반환한다.

        URL 구성, operation name 추출·검증, redaction 로깅을 한곳에 둔다.
        """
        url = f"{_GEMINI_BASE}/models/{self.settings.veo_model}:predictLongRunning"
        # Veo 제출은 간헐 400(동일 요청 400<->200 비결정, 2026-06-15 실측)을 견디기 위해
        # 429/5xx에 더해 400도 일시 오류로 재시도한다 — extend 다중 비트 완주율 확보.
        data = _send_json(
            lambda: self._client().post(
                url, headers=_gemini_headers(self.settings), json=body
            ),
            "Veo 작업 제출",
            sleep=self._sleep,
            max_transient_retries=_MAX_TRANSIENT_RETRIES,
            retry_400=True,
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
    """대본 → 시작 프레임(프레임 클라이언트: Kontext) → Veo 영상 생성을 담당하는 파사드(facade)."""

    def __init__(
        self,
        settings: Settings,
        *,
        nano_client=None,
        veo_client=None,
        kling_client=None,
        tts_client=None,
        kling_lipsync_client=None,
        veo_fal_client=None,
        sleep=None,
    ):
        # 실연동 클라이언트는 주입 가능하게 받는다(테스트에서 fake 주입 → 네트워크 불요).
        # 주입이 없으면 각 실 경로(non-dry_run)에서 지연 생성한다.
        self.settings = settings
        self._nano_client = nano_client
        self._veo_client = veo_client
        # kling 백엔드(무음 영상 + 한국어 TTS 보이스오버)용 주입 클라이언트.
        self._kling_client = kling_client
        self._tts_client = tts_client
        # Kling LipSync 후처리(NUTTI_KLING_LIPSYNC=true)용 주입 클라이언트.
        # 미주입 시 백엔드(KlingVoiceoverBackend)가 실 경로에서 지연 생성한다.
        self._kling_lipsync_client = kling_lipsync_client
        # fal.ai Veo 3.1 백엔드(video_backend="veo_fal")용 주입 클라이언트.
        # 미주입 시 _produce_clips_veo_fal에서 지연 생성하고 finally에서 1회 닫는다.
        self._veo_fal_client = veo_fal_client
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
        # 시작 프레임은 백엔드 무관하게 FalKontextClient(fal.ai)로 만든다 → FAL_KEY 필수.
        # kling 백엔드는 영상 클립도 fal.ai이므로 FAL_KEY 1개로 프레임+영상 모두 커버한다.
        if self.settings.video_backend == "kling":
            if self._nano_client is None and not _usable_key(self.settings.fal_key):
                raise ValueError(
                    "FAL_KEY가 비어 있습니다 — kling 백엔드의 프레임(Kontext) 생성에 필수입니다."
                )
            # TTS 소스 키 검증: kling_tts에 따라 필요한 키가 다르다.
            # · gemini(기본): TTS도 Gemini generateContent라 GEMINI_API_KEY 재사용.
            # · elevenlabs: ELEVENLABS_API_KEY 필수(타 호스트 유출 금지 키).
            # · supertone: SUPERTONE_API_KEY 필수(supertoneapi.com 전용 키).
            # tts_client를 직접 주입하면 소스 무관하게 이 키 검사를 건너뛴다(테스트/대체 구현 허용).
            if self._tts_client is None:
                if self.settings.kling_tts == "elevenlabs":
                    if not _usable_key(self.settings.elevenlabs_api_key):
                        raise ValueError(
                            "ELEVENLABS_API_KEY가 비어 있습니다 — "
                            "kling 백엔드의 TTS(kling_tts=elevenlabs)에 필수입니다."
                        )
                elif self.settings.kling_tts == "supertone":
                    if not _usable_key(self.settings.supertone_api_key):
                        raise ValueError(
                            "SUPERTONE_API_KEY가 비어 있습니다 — "
                            "kling 백엔드의 TTS(kling_tts=supertone)에 필수입니다."
                        )
                elif not _usable_key(self.settings.gemini_api_key):
                    raise ValueError("GEMINI_API_KEY가 비어 있습니다 — kling 백엔드의 TTS에 필수입니다.")
            if self._kling_client is None and not _usable_key(self.settings.fal_key):
                raise ValueError("FAL_KEY가 비어 있습니다 — kling 백엔드(dry_run=False) 시 필수입니다.")
            # LipSync 후처리(kling_lipsync=true)도 fal.ai 큐를 쓰므로 FAL_KEY 요구는 위에서 충족된다.
            return
        # veo_fal 백엔드: 시작 프레임(Kontext=FAL_KEY)과 영상 생성(fal.ai=FAL_KEY) 모두 FAL_KEY.
        # GEMINI_API_KEY 불요 — veo_fal은 FAL_KEY 하나로 프레임+영상 모두 처리한다.
        if self.settings.video_backend == "veo_fal":
            if self._nano_client is None and not _usable_key(self.settings.fal_key):
                raise ValueError(
                    "FAL_KEY가 비어 있습니다 — veo_fal 백엔드의 프레임(Kontext) 생성에 필수입니다."
                )
            if self._veo_fal_client is None and not _usable_key(self.settings.fal_key):
                raise ValueError(
                    "FAL_KEY가 비어 있습니다 — veo_fal 백엔드(dry_run=False) 시 필수입니다."
                )
            return
        # 기본 veo 백엔드: 프레임은 Kontext(FAL_KEY), 영상은 Gemini Veo(GEMINI_API_KEY).
        if self._nano_client is None and not _usable_key(self.settings.fal_key):
            raise ValueError(
                "FAL_KEY가 비어 있습니다 — 시작 프레임(Kontext) 생성에 필수입니다."
            )
        if self._veo_client is None and not _usable_key(self.settings.gemini_api_key):
            raise ValueError("GEMINI_API_KEY가 비어 있습니다 — veo 백엔드(dry_run=False) 시 필수입니다.")

    def produce(self, script: Script) -> VideoAsset:
        """시작 프레임 → 첫 클립 + extend 체이닝(veo) → VideoAsset 반환.

        veo 경로: 대본 비트(`script.beats`)가 N개면 첫 비트로 8초 클립을 만들고 이후
        비트를 extend로 이어 붙여 단일 연속 영상(총 8+7*(N-1)초)을 만든다. kling 경로는
        비트별 무음 클립 + TTS 보이스오버를 ffmpeg로 스티칭한다. 비트가 없으면 body
        단일컷(8초)으로 폴백한다. 실 경로의 정확한 길이는 _produce_clips가 돌려준 값으로
        덮어쓰고, 아래 duration은 dry_run·사전 추정용 계산이다(백엔드별로 분기).
        """
        # 실 경로면 시작 전에 필수 키를 검증(미설정 시 빠르게 실패).
        self.validate_config()
        beats = self._beats(script)
        # veo는 첫 8초 + 비트마다 +7초(extend), kling/그 외는 비트당 8초 가정.
        if self.settings.video_backend == "veo":
            duration = _veo_total_sec(len(beats))
        else:
            duration = _CLIP_SEC * len(beats)

        if self.settings.dry_run:
            log.info("dry_run.video", script_id=script.id, beats=len(beats))
            frame_path = f"data/dry_run/frame_{script.id}.jpg"
            video_path = f"data/dry_run/video_{script.id}.mp4"
            return VideoAsset(
                script_id=script.id,
                frame_image_path=frame_path,
                video_path=video_path,
                final_url=video_path,
                duration_sec=duration,
            )

        # 편별 스타일(의상·장소)은 여기서 정확히 한 번 계산해 프레임과 비트 클립에
        # 같은 값을 명시적으로 전달한다 — 두 곳에서 독립 계산하면 향후 호출 경로가
        # 갈릴 때 프레임과 클립의 장면이 어긋날 수 있다(리뷰 지적, PR #52).
        style = pick_episode_style(script.id)
        frame_path = self._generate_frame(script, style)
        # 실 경로의 총길이는 위 사전 추정 대신 백엔드가 돌려준 실측값으로 덮어쓴다 —
        # veo는 8+7*(N-1)초, kling은 mux `-shortest`로 음성 길이에 맞춰진 실측 총길이.
        video_path, duration = self._produce_clips(frame_path, beats, style)
        return VideoAsset(
            script_id=script.id,
            frame_image_path=frame_path,
            video_path=video_path,
            final_url=video_path,
            duration_sec=duration,
        )

    @staticmethod
    def _beats(script: Script) -> list[str]:
        """영상 비트 리스트를 만든다. `script.beats` 우선, 없으면 body/topic 단일 비트.

        공백·빈 문자열 비트는 제거한다(Veo에 빈 대사 클립을 만들지 않기 위함).
        """
        beats = [b.strip() for b in (script.beats or []) if b and b.strip()]
        if beats:
            return beats
        return [script.body.strip() or script.topic]

    def _produce_clips(
        self, frame_path: str, beats: list[str], style: EpisodeStyle
    ) -> tuple[str, float]:
        """비트별 클립을 생성해 ffmpeg로 이어붙인 (최종 경로, 실측 총길이초)를 반환한다(백엔드 분기).

        `video_backend`가 "kling"이면 무음 Kling 영상 + 한국어 TTS 보이스오버 백엔드로
        비트 클립을 만든다(편별 스타일 미적용 — 프롬프트 체계가 달라 veo 전용).
        그 외(기본 "veo")는 Veo 네이티브 음성 경로를 쓰고 스타일을 첫 비트 프롬프트에 반영한다.
        두 경로 모두 실측/계산된 총길이초를 함께 돌려준다 — kling은 클립 길이가 음성에
        맞춰지고, veo는 첫 8초 + 비트마다 +7초(extend)로 길이가 정해진다.
        """
        if self.settings.video_backend == "kling":
            return self._produce_clips_kling(frame_path, beats)
        if self.settings.video_backend == "veo":
            return self._produce_clips_veo(frame_path, beats, style)
        if self.settings.video_backend == "veo_fal":
            return self._produce_clips_veo_fal(frame_path, beats, style)
        # Settings.video_backend는 Literal["veo","kling","veo_fal"]이므로 여기는 도달 불가.
        # 직접 객체 변조·테스트 주입 등 런타임 우회 대비용 명시적 거부.
        # 설정 값을 메시지에 포함하면 monkey-patch된 임의 문자열이 로그/텔레그램으로 누출될 수 있다.
        raise ValueError(
            "알 수 없는 video_backend 값입니다 — 'veo', 'kling', 'veo_fal' 중 하나여야 합니다."
        )

    def _produce_clips_kling(self, frame_path: str, beats: list[str]) -> tuple[str, float]:
        """Kling 무음 + 한국어 TTS 백엔드로 비트 클립을 만들고 스티칭한다.

        무거운 의존(fal/TTS 클라이언트)을 import-time에 끌어오지 않도록 지연 import한다
        (httpx·imageio_ffmpeg와 동일한 lazy 패턴 → veo-only 경로의 import 비용 0).
        백엔드가 돌려준 실측 총길이초를 함께 반환해 duration_sec을 정확히 채운다.
        """
        from nutti.integrations.video_kling import KlingVoiceoverBackend

        backend = KlingVoiceoverBackend(
            self.settings,
            kling_client=self._kling_client,
            tts_client=self._tts_client,
            kling_lipsync_client=self._kling_lipsync_client,
            sleep=self._sleep,
        )
        clips, total_sec = backend.produce_beat_clips(frame_path, beats)
        return self._stitch(clips), total_sec

    def _produce_clips_veo_fal(
        self, frame_path: str, beats: list[str], style: EpisodeStyle
    ) -> tuple[str, float]:
        """fal.ai Veo 3.1로 비트마다 같은 시작 프레임에서 클립을 생성하고 스티칭한다.

        Gemini API Veo와 달리 fal Veo는 extend 엔드포인트를 미노출하므로, 비트별 독립
        클립을 생성한 뒤 _stitch로 합친다(Kling 스티칭 패턴과 동일). 프롬프트는
        VeoPromptBuilder.build_beat를 재사용해 마스코트 외형·목소리·연출 일관성을 유지한다.
        총길이 = _CLIP_SEC × len(beats)(각 클립 8초).

        FalVeoClient는 주입분 우선, 없으면 지연 생성하고 finally에서 소유분만 닫는다
        (_produce_clips_veo의 owned 패턴과 동일).
        """
        from nutti.integrations.video_veo_fal import FalVeoClient

        builder = VeoPromptBuilder()
        client = self._veo_fal_client
        owned = None
        if client is None:
            client = owned = FalVeoClient(self.settings, sleep=self._sleep)
        clips: list[str] = []
        try:
            for i, beat in enumerate(beats, start=1):
                # 정면 1인 발화(off_screen_interviewer=False) — 인터뷰 마이크 연출 제거
                # (2026-06-16 PO 피드백: 마이크 구도 아예 삭제).
                prompt = builder.build_beat(beat, off_screen_interviewer=False, style=style)
                clip_path = client.generate(frame_path, prompt)
                log.info("video.veo_fal.clip.done", path=clip_path, beat=i, of=len(beats))
                clips.append(clip_path)
        except BaseException:
            # 중도 실패 시 이미 받은 비트 클립(각 수백 MB)이 media_dir에 영구 잔존하지
            # 않도록 정리한다(Kling 스티칭 경로의 누수 방어와 동일).
            for done in clips:
                try:
                    Path(done).unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        finally:
            if owned is not None:
                _close_owned(owned)
        # 비트 클립(8초 고정)의 앞뒤 침묵을 잘라 비트 사이 공백을 줄인다
        # (2026-06-16 PO 피드백: 비트 사이 공백이 너무 길다). 트림 실패분은 원본·8초로 폴백.
        trimmed: list[str] = []
        total = 0.0
        for clip in clips:
            path, sec = self._trim_to_speech(clip)
            trimmed.append(path)
            total += sec if sec is not None else _CLIP_SEC
        return self._stitch(trimmed), total

    def _trim_to_speech(self, clip: str) -> tuple[str, float | None]:
        """클립에서 발화 구간만 남기고 앞뒤 침묵을 잘라 (새 경로, 길이초)를 반환한다.

        veo_fal 비트 클립은 8초 고정이라 짧은 대사 뒤에 긴 침묵이 남는다 — 그대로
        이어붙이면 비트 사이 공백이 길어진다(PO 피드백). ffmpeg silencedetect로 앞/뒤
        침묵 경계를 찾아 발화 구간 + 짧은 여유만 남긴다. 검출·트림 실패나 전구간 침묵 등
        이상 시 (원본 경로, None)을 돌려준다 — 더미 경로·예외에도 파이프라인이 안전하게
        진행되도록(단위 테스트의 가짜 클립 경로 포함).
        """
        import re
        import subprocess

        try:
            import imageio_ffmpeg

            ff = imageio_ffmpeg.get_ffmpeg_exe()
            probe = subprocess.run(
                [ff, "-hide_banner", "-i", clip, "-af",
                 "silencedetect=noise=-30dB:d=0.4", "-f", "null", "-"],
                capture_output=True,
            )
            err = probe.stderr.decode("utf-8", "replace")
            dm = re.search(r"Duration:\s*(\d+):(\d+):([0-9.]+)", err)
            if dm is None:
                return clip, None
            dur = int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3))
            starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", err)]
            ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", err)]
            # 앞 침묵: 0 근처에서 시작하는 첫 침묵의 끝이 발화 시작점.
            start_t = 0.0
            if starts and starts[0] <= 0.3 and ends:
                start_t = max(0.0, ends[0] - 0.10)
            # 뒤 침묵: EOF까지 이어지는 마지막 침묵의 시작이 발화 끝점.
            end_t = dur
            if starts:
                last = starts[-1]
                trails = (not ends) or ends[-1] <= last + 0.05 or ends[-1] >= dur - 0.05
                if last > start_t and trails:
                    end_t = min(dur, last + 0.35)
            out_sec = end_t - start_t
            if out_sec < 0.8 or end_t <= start_t:
                return clip, None  # 과도 트림 방지(전구간 침묵·검출 이상)
            # 실제로 잘라낼 무음이 0.5초 미만이면 재인코딩하지 않고 원본 유지 — Veo 클립은
            # 룸톤이 끝까지 깔려 데드에어가 거의 없다(2026-06-16 실측). 의미 있는 무음이
            # 있을 때만 트림해 무익한 재인코딩·중복 파일 생성을 막는다.
            if (dur - out_sec) < 0.5:
                return clip, None
            out = str(Path(self.settings.nutti_media_dir) / f"veo_fal_trim_{uuid4().hex[:8]}.mp4")
            cut = subprocess.run(
                [ff, "-y", "-hide_banner", "-ss", f"{start_t:.3f}", "-i", clip,
                 "-t", f"{out_sec:.3f}", "-c:v", "libx264", "-c:a", "aac", out],
                capture_output=True,
            )
            if cut.returncode != 0 or not Path(out).exists():
                return clip, None
            return out, out_sec
        except Exception:
            # 트림은 품질 개선용 best-effort — 어떤 실패도 원본 클립으로 폴백한다.
            return clip, None

    def _produce_clips_veo(
        self, frame_path: str, beats: list[str], style: EpisodeStyle
    ) -> tuple[str, float]:
        """첫 비트를 8초 클립으로 생성한 뒤 이후 비트를 Veo extend로 이어 붙여
        하나의 연속 영상(점프컷 없음)을 만들고 (최종 경로, 총길이초)를 반환한다.

        첫 클립은 시작 프레임 + 편별 스타일(의상·장소)을 쓴 image-to-video다. 이후
        비트는 직전 클립을 입력으로 한 extend라, 각 호출이 누적된 전체 영상을 돌려주므로
        마지막 결과가 최종 영상이다(별도 스티칭 불요). 배경·의상은 첫 클립에서 정해져
        extend 구간에 자동 계승되므로 extend 프롬프트(build_extend_beat)는 이어지는
        동작·발화만 묘사한다. 음성 일관성은 모든 프롬프트의 고정 목소리 묘사(_VOICE)와
        extend의 연속 발화 유도로 통제한다. Veo 클라이언트는 한 번만 확보해 재사용하고,
        자체 생성분만 finally에서 정확히 1회 닫는다(주입분은 소유자가 닫음 — 풀 누수 방지).

        extend는 Fast/Standard 모델만 지원하므로, 다중 비트 + Lite 조합은 과금되는
        제출 전에 명확한 ValueError로 빠르게 막는다(라이브에서 400으로 늦게 실패 방지).
        """
        if len(beats) > 1 and "lite" in self.settings.veo_model.lower():
            raise ValueError(
                "Veo extend(다중 비트 연속 영상)는 Lite 모델을 지원하지 않습니다 — "
                "NUTTI_VEO_MODEL을 fast 또는 standard로 설정하세요."
            )
        builder = VeoPromptBuilder()
        client = self._veo_client
        owned = None
        if client is None:
            client = owned = VeoClient(self.settings, sleep=self._sleep)
        try:
            if len(beats) == 1:
                # 단일 비트는 연장 없이 generate 한 클립을 그대로 쓴다(다운로드 경로).
                video_path = client.generate(
                    frame_path, builder.build_beat(beats[0], style=style)
                )
                log.info("video.veo.clip.done", path=video_path, beat=1, of=1)
                return video_path, _veo_total_sec(1)
            # 다중 비트: 첫 클립 URI → extend로 URI 체이닝(중간 다운로드 없음) → 마지막
            # 누적 URI만 1회 다운로드. extend 입력은 URI 참조여야 한다(인라인 base64 거부, 실측).
            uri = client._generate_uri(frame_path, builder.build_beat(beats[0], style=style))
            log.info("video.veo.clip.done", beat=1, of=len(beats))
            for i, beat in enumerate(beats[1:], start=2):
                uri = client.extend(uri, builder.build_extend_beat(beat))
                log.info("video.veo.extend.done", beat=i, of=len(beats))
            video_path = client._download(uri)
        finally:
            if owned is not None:
                _close_owned(owned)
        return video_path, _veo_total_sec(len(beats))

    def _stitch(self, clips: list[str]) -> str:
        """여러 8초 클립을 ffmpeg로 이어붙여 하나의 MP4로 만든다(재인코딩 concat).

        클립이 1개면 스티칭 없이 그대로 반환한다. ffmpeg 바이너리는 imageio-ffmpeg가
        번들한 것을 쓴다(시스템 설치 불요). 실패(ffmpeg 비정상 종료·미설치)는
        VideoRenderError 계약으로 변환하며, 입력 경로가 박힐 수 있는 stderr 원문은
        노출하지 않고 예외 타입명만 남긴다(redaction).
        """
        if len(clips) == 1:
            return clips[0]
        import subprocess

        import imageio_ffmpeg

        out_path = Path(self.settings.nutti_media_dir) / f"video_{uuid4().hex[:12]}.mp4"
        inputs: list[str] = []
        for clip in clips:
            inputs += ["-i", clip]
        streams = "".join(f"[{i}:v][{i}:a]" for i in range(len(clips)))
        filt = f"{streams}concat=n={len(clips)}:v=1:a=1[v][a]"
        cmd = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            *inputs,
            "-filter_complex",
            filt,
            "-map",
            "[v]",
            "-map",
            "[a]",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise VideoRenderError(f"영상 스티칭 실패: {type(exc).__name__}") from None
        log.info("video.stitched", path=str(out_path), clips=len(clips))
        return str(out_path)

    def _generate_frame(self, script: Script, style: EpisodeStyle) -> str:
        """프레임 클라이언트(Kontext)로 시작 프레임을 생성한다(마스코트 레퍼런스 이미지 첨부).

        `style`은 produce()에서 한 번 계산된 편별 스타일 — 비트 클립과 동일한
        의상·장소가 프레임에 들어가야 장면이 이어진다.
        주입된 클라이언트는 소유자가 닫고, 여기서 만든 것만 finally에서 닫는다
        (httpx 연결 풀 누수 방지). 주입분은 owned=None으로 둬 close하지 않는다.
        nano_client / self._nano_client 이름은 테스트 더블(FakeNanoBananaClient) 호환을
        위해 유지한다 — 덕타이핑으로 generate_frame 시그니처만 맞으면 동작한다.
        """
        from nutti.integrations.image_kontext import FalKontextClient

        client = self._nano_client
        owned = None
        if client is None:
            client = owned = FalKontextClient(self.settings, sleep=self._sleep)
        try:
            path = client.generate_frame(
                self._frame_prompt(script, style),
                reference_image_path=self.settings.nutti_mascot_image or None,
            )
        finally:
            if owned is not None:
                _close_owned(owned)
        log.info("video.frame.done", script_id=script.id)
        return path

    @staticmethod
    def _frame_prompt(script: Script, style: EpisodeStyle) -> str:
        """시작 프레임 생성용 장면 프롬프트(마스코트·세로 9:16·금지 요소 명시).

        `style`은 호출부(produce)가 한 번 계산해 비트 클립과 공유하는 편별
        의상·장소 — 여기서 독립 계산하지 않는다(프레임-클립 장면 일치 계약).
        주제도 AI 생성 텍스트이므로 `_sanitize_prompt_text`로 정제해 삽입한다
        (작은따옴표 치환 + 길이 제한 — 간접 프롬프트 주입 심층 방어).
        """
        topic = _sanitize_prompt_text(script.topic, _MAX_TOPIC_CHARS)
        # ===================== PO 수정 구역 (첫 장면 비주얼) =====================
        # 영상 "첫 장면의 구도·표정·마이크 연출"을 바꾸려면 아래 영어 묘사를 고친다.
        # 배경·의상은 위 로테이션 리스트(PO 수정 구역 — 편별 연출 로테이션)에서 고친다.
        # 마스코트 외형 자체는 NUTTI_MASCOT_IMAGE(레퍼런스 이미지)가 결정한다 — 여긴 구도/연출.
        # ASCII 작은따옴표(') 금지(주입 방어 검증과 충돌). 한국어로 원하는 그림만 정해도 됨.
        # 리터럴 "9:16"·브랜드명은 화면 자막으로 렌더되므로 넣지 않는다(세로 비율은 Kontext
        # aspect_ratio 파라미터가 담당). 캐릭터는 "진짜 실사 강아지"로 못박아 인형탈 방지.
        return (
            "A photorealistic tall vertical portrait-orientation starting frame for a "
            f"short-form video: {_MASCOT_APPEARANCE}, wearing {style.outfit}, {style.setting}, "
            "looking straight at the camera with a calm, gentle, friendly face, ready to "
            f"talk directly to the camera. {_CINEMATIC_LOOK} "
            f"Scene context: {topic}. "
            "Absolutely no text, letters, numbers, words, captions, logos, brand names, or "
            "watermarks anywhere. No people, no humans in costume, no other animals. "
            "No microphone and no interview setup in frame."
        )
        # =================== PO 수정 구역 끝 (첫 장면 비주얼) ===================
