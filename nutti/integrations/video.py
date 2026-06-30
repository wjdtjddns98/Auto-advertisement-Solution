"""영상 생성 연동: FLUX Kontext(fal.ai 시작 프레임) → fal.ai Veo 3.1 image-to-video.

흐름: ① FalKontextClient(FLUX.1 Kontext pro)가 마스코트 레퍼런스 이미지를 편집해
시작 프레임 이미지를 생성해 로컬 저장 →
② VeoPromptBuilder가 대사를 작은따옴표로 인용한 프롬프트를 만들고 →
③ FalVeoClient(video_veo_fal)가 비트마다 같은 시작 프레임에서 8초 클립을 생성한 뒤
VideoStudio가 앞뒤 침묵을 트림하고 ffmpeg로 이어붙인다(_stitch).

이 모듈은 백엔드 무관 공통 헬퍼(VideoRenderError·HTTP/저장 헬퍼·프롬프트 빌더·
편별 스타일)와 파사드 VideoStudio를 담는다. 실 fal 클라이언트는 image_kontext.py·
video_veo_fal.py에 있고, fal 큐 공통 헬퍼는 _fal_common.py에 있다.

dry_run에서는 네트워크/키 없이 결정적 더미 경로를 채워 파이프라인을 검증한다.
모든 오류는 `VideoRenderError`(타임아웃은 `VideoTimeoutError`)로만 전파한다 —
HTTP 상태·전송·JSON 파싱·디스크 쓰기 실패 전부 포함(오케스트레이터 계약).
에러 메시지는 상태 코드/예외 타입명만 남기고 URL·request id·응답 본문은
노출하지 않는다(redaction).
"""

from __future__ import annotations

import time
import zlib
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import Script, VideoAsset

log = get_logger(__name__)

# 일시 오류(429 쿼터·5xx 백엔드 장애)의 최대 재시도 횟수와 backoff 기준(초).
# 폴링 윈도우에서 단 1회의 일시 오류로 작업을 영구 포기하지 않기 위한 장치다.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BACKOFF_SEC = 2.0
# 프롬프트에 삽입하는 AI 생성 텍스트의 길이 상한(주입 표면 제한).
# 비트 1개(8초)의 대사는 짧으므로 이 한도를 넘을 이유가 없다.
_MAX_DIALOGUE_CHARS = 500
_MAX_TOPIC_CHARS = 200
# 비트 1개(독립 클립)의 길이(초). veo_fal 경로는 비트마다 8초 클립을 만들어 스티칭한다.
_CLIP_SEC = 8.0

# 발화 끝 적응 트림(_trim_to_speech) 파라미터(2026-06-30 PO 실측 보정). Veo 8초 클립은
# 발화가 ~6초에 끝나도 뒤를 음악/앰비언스로 채워 무음이 안 생긴다 — 종전 silencedetect(EOF
# 무음) 방식이 발동 못 했다. 대신 RMS 엔벨로프를 떠 발화 본체 직후 "깊은 딥"(발화 끝)을 찾는다.
_TRIM_SR = 16000  # 엔벨로프 분석용 모노 다운샘플레이트(디코드 비용↓, 음성 대역 충분)
_TRIM_WIN = 0.25  # 엔벨로프 윈도 길이(초)
_TRIM_SPEECH_MIN = -24.0  # 이 dBFS를 넘으면 발화로 간주(발화 본체 식별·시작점)
_TRIM_ABS_CAP = -30.0  # 발화 끝 딥의 절대 바닥(이보다 조용해야 딥 후보)
_TRIM_DROP = 13.0  # 직전 발화 대비 낙폭(dB) — 이만큼 떨어지면 발화가 멈춘 것
# 딥 이후 이 dBFS를 넘는 구간이 있으면 발화 재개(=중간 멈춤)로 보고 그 딥을 기각한다.
# _TRIM_SPEECH_MIN(-24)보다 4dB 엄격한 건 **의도된 갭**: PO 실측상 Veo의 발화 본체는
# -12~-18 dBFS로 크고, 발화 후 잉여를 채우는 tail-fill은 -22~-31 dBFS다. 재개 기준을
# 그 사이(-20)에 둬야 tail-fill(<-20)은 "재개 아님"으로 통과시켜 트림하고, 진짜 발화
# 재개(-12~-18 > -20)는 "재개"로 잡아 중간 멈춤 딥을 기각한다. -24로 낮추면 tail-fill
# (-22.8 실측치)이 재개로 오인돼 검증된 클립의 트림이 깨진다(절대 낮추지 말 것).
_TRIM_RESUME = -20.0
_TRIM_LOOKBACK = 4  # 직전 발화 레벨 참조 윈도 개수(=1초)
_TRIM_MIN_SPEECH = 2.5  # 발화 시작 후 이 초 이전의 딥은 무시(훅 중 멈춤 오검출 방지)
_TRIM_PAD = 0.15  # 발화 끝 뒤 남길 여유(초) — 끝음절 보존
# 화면 자막(깨진 한글 텍스트) 억제용 negative_prompt는 이제 설정값
# `Settings.veo_fal_negative_prompt`로 단일화되어 FalVeoClient._submit이 fal에 직접
# 보낸다(2026-06-18). 프롬프트 본문의 "no on-screen text" 지시와 이중 방어를 이룬다.


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
    CRC32 기반 결정적 선택 패턴(같은 입력 → 항상 같은 결과).
    """
    outfit_idx = zlib.crc32(f"outfit:{script_id}".encode()) % len(_EPISODE_OUTFITS)
    setting_idx = zlib.crc32(f"setting:{script_id}".encode()) % len(_EPISODE_SETTINGS)
    return EpisodeStyle(_EPISODE_OUTFITS[outfit_idx], _EPISODE_SETTINGS[setting_idx])


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
    - 카메라는 고정(locked-off)·무빙 없음 — 흔들림/컷 전환 방지("tripod" 단어는 화면에
      삼각대로 렌더되므로 프롬프트에서 제외, 2026-06-29 실측).
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
    #   임의로 박는 걸 막는 핵심 방어 — 함부로 빼지 말 것(settings.veo_fal_negative_prompt와 이중 방어)
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
        "Voice (must be EXACTLY the same single voice in every clip of this series, like "
        "one specific recognizable person with a fixed vocal fingerprint): a bright, "
        "cute Little girl Korean voice, sounding about 6 years old, slightly high-pitched, "
        "cheeky and energetic, with a warm soft timbre and a consistent speaking rhythm at "
        "a lively natural pace. Keep the identical timbre, pitch, accent, and speaking speed "
        "in every clip. Keep this exact same voice even on excited, exclamatory, or "
        "call-to-action lines: do not raise the pitch, do not get louder, do not turn into "
        "an excited announcer or a promotional voice-over, and never switch to a different "
        "speaker or a different age — every line, including the final call-to-action, must "
        "sound like the exact same little girl speaking in the same calm, even tone as the "
        "earlier lines. "
        # 발화 후 잉여 구간 BGM 채움 억제 — Veo가 대사가 끝난 뒤 남는 시간을 배경음악으로
        # 채우면 무음 트림이 발화 끝을 못 잡아 끝부분 헛짓이 남는다(2026-06-29 PO 실측).
        "This single spoken voice is the only audio: there is no background music, "
        "instrumental, soundtrack, jingle, or sound effects at any point. After the puppy "
        "finishes the last word, the audio simply stays quiet with natural room tone — do "
        "not fill the remaining time with any music or sound."
    )
    _MIC = (
        "A handheld interview microphone is pointed at the puppy from off-screen; "
        "the person holding it stays completely out of frame."
    )
    _SPEAKING_OFF = "speaking in Korean to an off-screen interviewer"
    _SPEAKING_DIRECT = "speaking in Korean directly to the camera"
    # "tripod" 단어를 넣으면 Veo가 화면에 삼각대를 렌더한다(2026-06-29 실측) — 단어를
    # 빼고 "고정 카메라"는 fixed/static/no movement로만 지시한다.
    _CAMERA = "Camera: locked-off static shot, fixed framing, no camera movement."
    # 비트 클립이 독립 생성돼 끝 자세가 제각각이면 다음 클립과 점프가 생긴다(PO 피드백
    # 2026-06-29). 자세를 처음부터 끝까지 고정하고, 끝을 페이드 없이 또렷한 프레임으로
    # 마무리하게 해 프레임 체이닝(끝 프레임→다음 시작 프레임)이 안정적으로 물리도록 한다.
    # _CONTINUITY: 클립이 독립 생성돼 비트마다 의상·외형이 달라지면(실측 2026-06-29:
    # 회색 후드 → 맨몸으로 점프) 경계에서 튄다. 의상·털·외형을 처음부터 끝까지 동일하게
    # 못박아 비트 간 점프를 줄인다(같은 style.outfit이 모든 비트에 들어가도 veo가 바꾸는
    # 경향에 대한 추가 방어).
    _CONTINUITY = (
        "Keep the exact same outfit and clothing on the puppy in every frame with no "
        "changes — do not add, remove, or alter any clothing mid-shot. Keep the identical "
        "fur color, markings, and overall appearance from the first frame to the last."
    )
    _MOTION_HOLD = (
        "The puppy stays in the exact same upright seated position for the entire shot, "
        "sitting still and centered, holding the same pose from the first frame to the "
        "last frame; it does not lie down, stand up, walk, or leave the frame. The clip "
        "ends on a clean, fully-lit, sharp frame with the puppy seated and centered — no "
        "fade-out, no dimming, no blur at the end."
    )
    # 끝프레임 고정(lock) 모드 전용 모션 지시(2026-06-29 PO: "모션홀드 풀어 생동감").
    # first-last-frame 모델이 시작·끝 프레임을 동일 마스코트 프레임으로 강제하므로, 중간에
    # 자유롭게 움직여도 클립은 항상 같은 끝 포즈로 수렴한다 — 정적인 _MOTION_HOLD 대신
    # 앉은 채 자연스러운 제스처를 허용해 생기를 준다. 단 화면 이탈·기립·눕기는 막아(막판
    # 이상행동 방지) 끝을 차분한 앉은 자세로 마무리하게 하고, 끝 페이드는 금지한다
    # (negative_prompt의 "lying down/walking out/camera movement" 억제와 이중 방어).
    _MOTION_LIVELY = (
        "The puppy stays seated and centered in frame the whole time but moves naturally "
        "and expressively as it talks — gentle head tilts, ear and body movements, "
        "blinking, and lively little gestures that bring energy to the shot. It never "
        "stands up, walks, lies down, hunches over, ducks its head down, curls forward, or "
        "leaves the frame. CRITICAL ending rule: for the final two to three seconds the "
        "puppy must be completely frozen and motionless — sitting perfectly upright and "
        "centered, facing straight forward, with absolutely no movement of the head, ears, "
        "body, paws, mouth, or expression. No shifting, no turning, no ducking, no leaning, "
        "no sudden motion, no extra gestures in this final hold. The clip must end on this "
        "fully frozen, calm, clean, fully-lit, razor-sharp frame — no fade-out, no dimming, "
        "no blur, no warping, no morphing, and no glitch at the end."
    )
    _NEGATIVE = (
        "The subject is a real live photorealistic puppy — never a mascot suit, fursuit, "
        "costume, person in a costume, or plush toy. Strictly no additional animals, no "
        "people. Absolutely no text, subtitles, captions, letters, numbers, words, logos, "
        "brand names, watermarks, or UI overlays anywhere in the frame."
    )
    # 마지막 비트(CTA) 전용 음성 앵커 — CTA 대사가 권유·느낌표 톤이라 Veo가 음성을 더
    # 들뜨거나 아나운서처럼 바꾸는 경향이 강하다(2026-06-29 PO 실측). 마지막 비트
    # 프롬프트에만 추가로 박아 앞 비트와 동일 화자·톤으로 못박는다(_VOICE와 이중 방어).
    _CTA_VOICE_ANCHOR = (
        "This is the final line of the series. Speak it in the exact same voice, pitch, "
        "age, and calm even tone as the previous clips — the same little girl, not louder, "
        "not more excited, not an announcer or promo voice. Do not change the speaker for "
        "this call to action."
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
        motion_release: bool = False,
        final_cta: bool = False,
    ) -> str:
        """비트 대사 한 토막으로 8초 단일컷 Veo 프롬프트를 만든다.

        페르소나·고정 목소리 묘사는 항상 포함되고, `style`이 주어지면 의상·장소상황
        문장이 추가된다(편 안에서 모든 비트가 같은 style을 받아 장면 연속성 유지).
        인터뷰 마이크 연출(_MIC)은 off_screen_interviewer=True일 때만 붙는다 —
        정면 발화 모드는 마이크 없는 1인 방송 톤.
        대사는 음성(spoken audio only)으로만 발화시키고 화면 자막을 금지한다 — Veo가
        한글 자막을 임의 렌더하면 깨진 글자로 나오기 때문(settings.veo_fal_negative_prompt와 이중 방어).
        대사는 `_sanitize_prompt_text`로 정제한다 — 작은따옴표가 있으면 인용 구분자를
        탈출해 금지 지시(추가 동물·사람·텍스트 금지)를 덮어쓰는 주입이 가능하기 때문이다.
        `motion_release=True`(끝프레임 고정 모드 전용)면 정적인 _MOTION_HOLD 대신 자연스러운
        제스처를 허용하는 _MOTION_LIVELY를 써 생동감을 준다 — 끝 프레임이 모델로 고정되므로
        중간 모션을 풀어도 경계는 매끄럽다.
        `final_cta=True`(마지막 비트 전용)면 _CTA_VOICE_ANCHOR를 덧붙여 CTA 대사에서
        음성이 들뜨거나 화자가 바뀌는 경향을 추가로 억제한다(2026-06-29 PO).
        """
        dialogue = _sanitize_prompt_text(dialogue_text.strip() or "", _MAX_DIALOGUE_CHARS)
        speaking = self._SPEAKING_OFF if off_screen_interviewer else self._SPEAKING_DIRECT
        scene = ""
        if style is not None:
            scene = f"The puppy wears {style.outfit}, {style.setting}. "
        mic = f"{self._MIC} " if off_screen_interviewer else ""
        motion = self._MOTION_LIVELY if motion_release else self._MOTION_HOLD
        cta = f"{self._CTA_VOICE_ANCHOR} " if final_cta else ""
        return (
            f"A photorealistic shot of {self._PERSONA}, {speaking}, "
            f"saying (as spoken audio only, no on-screen text): '{dialogue}'. "
            f"{scene}{mic}"
            f"{self._VOICE} {cta}"
            f"{self._CAMERA} "
            f"{motion} "
            f"{self._CONTINUITY} "
            f"{_CINEMATIC_LOOK} "
            "Format: tall vertical portrait orientation, single continuous 8-second shot. "
            f"{self._NEGATIVE}"
        )


# NanoBananaClient(Gemini 이미지 생성)는 2026-06 PO 결정으로 FalKontextClient로 교체됨.
# 프레임 클라이언트는 nutti/integrations/image_kontext.py의 FalKontextClient를 사용한다.


class VideoStudio:
    """대본 → 시작 프레임(Kontext) → fal.ai Veo 3.1 영상 생성을 담당하는 파사드(facade)."""

    def __init__(
        self,
        settings: Settings,
        *,
        nano_client=None,
        veo_fal_client=None,
        sleep=None,
    ):
        # 실연동 클라이언트는 주입 가능하게 받는다(테스트에서 fake 주입 → 네트워크 불요).
        # 주입이 없으면 각 실 경로(non-dry_run)에서 지연 생성한다.
        self.settings = settings
        self._nano_client = nano_client
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
        # veo_fal 백엔드(단일 백엔드): 시작 프레임(Kontext=FAL_KEY)과 영상 생성(fal.ai=FAL_KEY)
        # 모두 FAL_KEY 하나로 처리한다. GEMINI_API_KEY 불요.
        if self._nano_client is None and not _usable_key(self.settings.fal_key):
            raise ValueError(
                "FAL_KEY가 비어 있습니다 — veo_fal 백엔드의 프레임(Kontext) 생성에 필수입니다."
            )
        if self._veo_fal_client is None and not _usable_key(self.settings.fal_key):
            raise ValueError(
                "FAL_KEY가 비어 있습니다 — veo_fal 백엔드(dry_run=False) 시 필수입니다."
            )

    def produce(self, script: Script) -> VideoAsset:
        """시작 프레임 → 비트별 fal.ai Veo 클립 → 스티칭 → VideoAsset 반환.

        veo_fal 경로: 대본 비트(`script.beats`)가 N개면 같은 시작 프레임에서 비트마다
        8초 클립을 만들어 ffmpeg로 이어붙인다. 비트가 없으면 body 단일컷(8초)으로 폴백한다.
        실 경로의 정확한 길이는 _produce_clips가 돌려준 값(트림 실측)으로 덮어쓰고,
        아래 duration은 dry_run·사전 추정용 계산이다(비트당 8초 가정).
        """
        # 실 경로면 시작 전에 필수 키를 검증(미설정 시 빠르게 실패).
        self.validate_config()
        beats = self._beats(script)
        # 비트당 8초 클립을 만들어 스티칭한다(실측 길이는 _produce_clips가 덮어쓴다).
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
        # 실 경로의 총길이는 위 사전 추정 대신 veo_fal이 돌려준 실측값(비트 클립 앞뒤
        # 침묵 트림 반영)으로 덮어쓴다.
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
        """비트별 클립을 생성해 ffmpeg로 이어붙인 (최종 경로, 실측 총길이초)를 반환한다.

        단일 백엔드 veo_fal: fal.ai Veo 3.1 네이티브 음성 경로로 비트마다 같은 시작
        프레임에서 8초 클립을 만들고, 편별 스타일(의상·장소)을 각 비트 프롬프트에 반영한다.
        앞뒤 침묵을 트림한 실측 총길이초를 함께 돌려준다.
        """
        return self._produce_clips_veo_fal(frame_path, beats, style)

    def _produce_clips_veo_fal(
        self, frame_path: str, beats: list[str], style: EpisodeStyle
    ) -> tuple[str, float]:
        """fal.ai Veo 3.1로 비트마다 같은 시작 프레임에서 클립을 생성하고 스티칭한다.

        fal Veo는 extend 엔드포인트를 미노출하므로, 비트별 독립 클립을 생성한 뒤
        _stitch로 합친다. 프롬프트는 VeoPromptBuilder.build_beat를 재사용해 마스코트
        외형·목소리·연출 일관성을 유지한다. 총길이는 각 클립의 트림 실측 합이다.

        FalVeoClient는 주입분 우선, 없으면 지연 생성하고 finally에서 소유분만 닫는다.
        """
        from nutti.integrations.video_veo_fal import FalVeoClient

        builder = VeoPromptBuilder()
        client = self._veo_fal_client
        owned = None
        if client is None:
            client = owned = FalVeoClient(self.settings, sleep=self._sleep)
        clips: list[str] = []
        # 가드된 프레임 체이닝용 임시 프레임(정리 대상). 원본 frame_path는 제외.
        chain_frames: list[str] = []
        # 끝프레임 고정 모드(2026-06-29 PO): 모든 비트가 같은 마스코트 프레임에서 시작·종료
        # 하도록 first/last 프레임을 frame_path로 고정한다 — 클립이 같은 포즈로 시작·끝나
        # 비트 경계가 항상 동일 프레임에서 만나 끊김이 없다. 체이닝(끝 프레임 추출)은 불요.
        lock = bool(self.settings.veo_fal_endframe_lock)
        # 영상 내 모든 비트(n1~n4)에 같은 seed를 줘 음색/비주얼 편차를 줄인다(2026-06-29 PO:
        # 음색 일관성 보강). 설정값(veo_fal_seed)이 없으면 이 영상용 seed 1개를 뽑아 모든 비트에
        # 재사용한다 — 영상 내 일관, 영상 간 다양성 유지. Veo가 seed로 오디오를 완전 통제하진
        # 않지만, 같은 seed + 같은 음색 프롬프트(_VOICE)면 비트 간 목소리가 더 비슷해진다.
        video_seed = self.settings.veo_fal_seed
        if video_seed is None:
            import random

            video_seed = random.randint(0, 2**31 - 1)
        # 각 비트의 시작 프레임. 기본 모드는 1번 비트가 마스코트 Kontext 프레임에서 시작하고
        # 이후 비트는 직전 클립의 끝 안정 프레임으로 이어 붙인다(체이닝). lock 모드는 항상
        # frame_path 고정.
        current_frame = frame_path
        try:
            for i, beat in enumerate(beats, start=1):
                # 정면 1인 발화(off_screen_interviewer=False) — 인터뷰 마이크 연출 제거
                # (2026-06-16 PO 피드백: 마이크 구도 아예 삭제).
                # lock 모드는 끝 프레임이 모델로 고정되므로 모션 제약을 풀어(_MOTION_LIVELY)
                # 생동감을 준다(2026-06-29 PO). 기본 image-to-video 경로는 _MOTION_HOLD 유지.
                prompt = builder.build_beat(
                    beat,
                    off_screen_interviewer=False,
                    style=style,
                    motion_release=lock,
                    final_cta=(i == len(beats)),
                )
                if lock:
                    # 시작·끝 모두 마스코트 프레임으로 고정(끝프레임 고정 모드).
                    clip_path = client.generate(
                        frame_path, prompt, last_frame_path=frame_path, seed=video_seed
                    )
                else:
                    clip_path = client.generate(current_frame, prompt, seed=video_seed)
                # 끝 잉여 구간(글리치·이상동작 온상) 강제 제거: 8초→약7초(2026-06-29 PO).
                # 트림 성공 시 원본 8초 클립은 즉시 삭제(잔존 방지). 이후 체이닝 끝프레임
                # 추출·무음 트림은 모두 트림된 클립 기준 — 글리치 구간이 다음 단계에도 안 샌다.
                tail = self.settings.veo_fal_clip_tail_trim_sec
                if tail > 0:
                    cut = self._trim_tail_fixed(clip_path, tail)
                    if cut != clip_path:
                        Path(clip_path).unlink(missing_ok=True)
                        clip_path = cut
                log.info("video.veo_fal.clip.done", path=clip_path, beat=i, of=len(beats))
                clips.append(clip_path)
                # 가드된 체이닝(기본 모드만): 다음 비트가 있으면 이 클립의 끝 안정 프레임을
                # 다음 시작 프레임으로 쓴다. 추출·품질 가드(검정/빈/가로) 실패 시 None → 원본
                # 마스코트 프레임으로 안전 폴백(망가진 프레임이 다음 클립에 누적되지 않게 하는
                # 핵심 가드). lock 모드는 끝프레임을 frame_path로 고정하므로 체이닝하지 않는다.
                if not lock and i < len(beats):
                    chained = self._chain_frame(clip_path)
                    if chained is not None:
                        chain_frames.append(chained)
                        current_frame = chained
                    else:
                        log.info("video.veo_fal.chain.fallback", beat=i)
                        current_frame = frame_path
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
            # 체이닝 임시 프레임은 generate에 base64로 이미 들어갔으니 더 필요 없다 — 정리.
            for cf in chain_frames:
                try:
                    Path(cf).unlink(missing_ok=True)
                except OSError:
                    pass
            if owned is not None:
                _close_owned(owned)
        # 비트 클립(8초 고정)의 앞뒤 침묵을 잘라 비트 사이 공백을 줄인다
        # (2026-06-16 PO 피드백: 비트 사이 공백이 너무 길다). 트림 실패분은 원본·8초로 폴백.
        trimmed: list[str] = []
        durations: list[float | None] = []
        total = 0.0
        for clip in clips:
            path, sec = self._trim_to_speech(clip)
            trimmed.append(path)
            durations.append(sec)
            total += sec if sec is not None else _CLIP_SEC
        # 트림으로 새로 만든 임시 파일(veo_fal_trim_*.mp4)은 스티칭 후 정리한다 — 원본
        # 비트 클립은 기존 정책대로 유지하고, 단일 비트라 _stitch가 그대로 돌려준 파일
        # (final)은 삭제 대상에서 제외한다(반환 파일 삭제 방지). 스티칭 실패 시에도 정리.
        final = None
        try:
            final = self._stitch(trimmed, durations)
            # total은 트림 클립 길이의 단순 합 = 디졸브 전 상한값이다. _stitch가 경계
            # 디졸브를 적용하면 실제 산출물은 (비트수-1)*crossfade_sec 만큼 짧다(0.25초
            # 기본이면 3비트당 0.5초). 여기서 산술 보정하지 않는 이유: 호출부는 _stitch가
            # 디졸브를 실제 적용했는지(ffmpeg 성공 여부) 모른다 — 실패해 concat 폴백하면
            # 보정값이 오히려 틀린다. duration_sec은 현재 metadata 전용(비즈니스 컷오프·
            # 과금에 미사용)이라 이 오차는 무해. 정확한 길이가 필요해지면 산술이 아니라
            # 최종 mp4를 ffprobe로 재측정해야 한다.
            return final, total
        finally:
            for orig, t in zip(clips, trimmed):
                if t != orig and t != final:
                    try:
                        Path(t).unlink(missing_ok=True)
                    except OSError:
                        pass

    def _trim_tail_fixed(self, clip: str, trim_sec: float) -> str:
        """클립 끝에서 trim_sec초를 강제로 잘라낸 새 클립 경로를 반환한다(무음 무관).

        veo_fal 비트 클립은 8초 고정인데, 끝 ~1초 잉여 구간에서 모델이 자세를 무너뜨리거나
        순간 글리치/이상동작을 내는 경향이 있다(2026-06-29 PO). 발화·무음 여부와 무관하게
        끝 trim_sec을 물리적으로 제거해 그 구간을 영상에서 배제한다(대사 끝이 약간 잘릴 수
        있음 — PO 수용). trim_sec<=0, 길이 측정 실패, 과도 트림(남는 길이<2s), 재인코딩
        실패 시 원본을 그대로 반환한다(파이프라인 안전 — 단위 테스트의 가짜 클립 포함).
        """
        if trim_sec <= 0:
            return clip
        import re
        import subprocess

        try:
            import imageio_ffmpeg

            ff = imageio_ffmpeg.get_ffmpeg_exe()
            probe = subprocess.run([ff, "-hide_banner", "-i", clip], capture_output=True)
            err = probe.stderr.decode("utf-8", "replace")
            dm = re.search(r"Duration:\s*(\d+):(\d+):([0-9.]+)", err)
            if dm is None:
                return clip
            dur = int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3))
            keep = dur - trim_sec
            if keep < 2.0:
                return clip  # 과도 트림 방지(짧은 클립·측정 이상)
            out = str(Path(self.settings.nutti_media_dir) / f"veo_fal_tail_{uuid4().hex[:8]}.mp4")
            cut = subprocess.run(
                [ff, "-y", "-hide_banner", "-i", clip, "-t", f"{keep:.3f}",
                 "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", "-c:a", "aac", out],
                capture_output=True,
            )
            if cut.returncode != 0 or not Path(out).exists():
                return clip  # 트림 실패 — 원본 유지
            return out
        except Exception:
            # best-effort — 어떤 실패도 원본 클립으로 폴백한다.
            return clip

    # 프레임 체이닝 가드 임계 — mp4 추출 PNG는 보통 수백 KB~MB. 이 미만이면 검정/실패
    # 프레임 의심. image_kontext._MIN_FRAME_BYTES(51_200)와 같은 종류의 휴리스틱이되,
    # ffmpeg 추출 PNG는 압축 특성이 달라 더 낮은 임계를 둔다(가로/검정만 거른다).
    _MIN_CHAIN_FRAME_BYTES = 20_000

    def _chain_frame(self, clip_path: str) -> str | None:
        """클립 끝의 안정 프레임을 추출해 품질 가드를 통과하면 PNG 경로를 반환한다.

        다음 비트 클립의 시작 프레임으로 쓰여 비트 경계 자세 점프를 줄인다(프레임 체이닝).
        끝에서 약간 앞(~0.35s)을 뽑아 클립 마무리의 페이드·잔여 움직임을 피한다. 추출 실패나
        검정/빈/가로 프레임 등 의심스러우면 None을 반환 — 호출부가 원본 마스코트 프레임으로
        안전 폴백해 망가진 프레임이 다음 클립에 누적되지 않게 한다(best-effort 품질 개선).
        """
        try:
            if not clip_path or not Path(clip_path).exists():
                return None
            import subprocess

            import imageio_ffmpeg

            ff = imageio_ffmpeg.get_ffmpeg_exe()
            out = str(Path(self.settings.nutti_media_dir) / f"chain_{uuid4().hex[:8]}.png")
            # 끝에서 0.35초 앞 프레임(발화 직후 안정 구간, 페이드 회피).
            # timeout 필수 — 손상 MP4에서 ffmpeg이 멈추면 timeout 없이는 파이프라인 전체가
            # 무기한 블록된다. 단일 프레임 추출은 1초 미만이 정상. TimeoutExpired는
            # Exception 서브클래스라 아래 except가 잡아 None(원본 프레임 폴백)으로 처리한다.
            res = subprocess.run(
                [ff, "-y", "-hide_banner", "-sseof", "-0.35", "-i", clip_path,
                 "-frames:v", "1", out],
                capture_output=True,
                timeout=15,
            )
            if res.returncode != 0 or not Path(out).exists():
                return None
            if self._reject_chain_frame(out):
                try:
                    Path(out).unlink(missing_ok=True)
                except OSError:
                    pass
                return None
            return out
        except Exception:
            # 체이닝은 best-effort — 어떤 실패도 None(원본 프레임 폴백)으로 안전 처리.
            return None

    def _reject_chain_frame(self, path: str) -> bool:
        """체이닝 프레임이 퇴화(검정/빈/가로)면 True를 반환한다.

        image_kontext._reject_reason과 같은 휴리스틱: 바이트가 너무 작으면 검정/실패 의심,
        PNG 해상도가 세로(height>width)가 아니면 레퍼런스 미적용 placeholder로 보고 거부한다.
        """
        try:
            data = Path(path).read_bytes()
        except OSError:
            return True
        if len(data) < self._MIN_CHAIN_FRAME_BYTES:
            return True
        from nutti.integrations.image_kontext import _png_dimensions

        dims = _png_dimensions(data)
        if dims is not None:
            width, height = dims
            if height <= width:  # 세로(9:16)가 아니면 거부
                return True
        return False

    def _trim_to_speech(self, clip: str) -> tuple[str, float | None]:
        """클립에서 발화 구간만 남기고 끝 잉여(글리치·tail-fill)를 잘라 (새 경로, 길이초)를 반환.

        veo_fal 비트 클립은 8초 고정인데 발화는 보통 6~7초에 끝난다. 그런데 Veo가 발화 후
        남는 잉여를 음악/앰비언스로 채워(2026-06-30 PO 실측) 무음이 생기지 않는다 — 종전
        silencedetect(EOF 무음) 방식은 이 채움 때문에 발동 못 했다. 대신 0.25초 RMS 엔벨로프를
        떠 **발화 본체 직후 첫 깊은 딥**(직전 발화 대비 큰 낙폭 + 절대 바닥, 그리고 그 뒤로
        발화가 재개되지 않음)을 발화 끝으로 잡는다. 딥은 발화 길이를 따라 이동하므로 대본이
        길든 짧든 대사를 자르지 않고 끝 잉여(글리치 온상)만 제거한다.

        디코드·검출 실패나 발화 미검출 등 이상 시 (원본 경로, 실측/None)을 돌려준다 — 더미
        경로·예외에도 파이프라인이 안전하게 진행되도록(단위 테스트의 가짜 클립 경로 포함).
        """
        import array
        import math
        import subprocess

        try:
            import imageio_ffmpeg

            ff = imageio_ffmpeg.get_ffmpeg_exe()
            # 엔벨로프 분석용으로 모노 16kHz PCM을 1패스 디코드(silencedetect 다중 호출보다 효율).
            dec = subprocess.run(
                [ff, "-hide_banner", "-i", clip, "-ac", "1", "-ar",
                 str(_TRIM_SR), "-f", "s16le", "-"],
                capture_output=True,
            )
            pcm = array.array("h")
            pcm.frombytes(dec.stdout)
            if len(pcm) < _TRIM_SR:  # 1초 미만(빈/실패 디코드·더미 경로) — 트림 불가
                return clip, None
            dur = len(pcm) / _TRIM_SR
            win = int(_TRIM_SR * _TRIM_WIN)
            # 윈도별 RMS를 dBFS로 변환한 엔벨로프.
            env = [
                10 * math.log10(
                    sum(x * x for x in pcm[i:i + win]) / win / (32768.0**2) + 1e-12
                )
                for i in range(0, len(pcm) - win + 1, win)
            ]
            # 발화 시작 = 첫 발화 윈도(앞 룸톤/침묵 트림). 발화 자체가 없으면 폴백.
            start_idx = next((j for j, v in enumerate(env) if v > _TRIM_SPEECH_MIN), None)
            if start_idx is None:
                return clip, dur
            start_t = max(0.0, start_idx * _TRIM_WIN - 0.10)
            # 발화 끝 = 발화 본체 직후 첫 깊은 딥(직전 1초 대비 _TRIM_DROP 이상 낙폭 + 절대 바닥)
            # 이면서 그 뒤로 발화가 재개되지 않는(=중간 멈춤이 아닌) 지점. + 짧은 여유를 남긴다.
            end_t = dur
            for j in range(len(env)):
                t = j * _TRIM_WIN
                if t < start_t + _TRIM_MIN_SPEECH:  # 발화 본체 최소 길이 확보 후 탐색
                    continue
                prev = env[max(0, j - _TRIM_LOOKBACK):j]
                if not prev or max(prev) < _TRIM_SPEECH_MIN:
                    continue
                if env[j] <= _TRIM_ABS_CAP and env[j] <= max(prev) - _TRIM_DROP:
                    after = env[j + 1:]
                    if not after or max(after) <= _TRIM_RESUME:
                        end_t = min(dur, t + _TRIM_PAD)
                        break
            out_sec = end_t - start_t
            if out_sec < 0.8 or end_t <= start_t:
                return clip, dur  # 과도 트림 방지(검출 이상) — 실측 길이 유지
            # 실제로 잘라낼 구간이 0.5초 미만이면 재인코딩하지 않고 원본 유지 — 무익한
            # 재인코딩·중복 파일 생성을 막는다(발화가 8초를 꽉 채운 긴 대본 등).
            if (dur - out_sec) < 0.5:
                return clip, dur  # 트림 안 함 — 실측 길이 유지
            out = str(Path(self.settings.nutti_media_dir) / f"veo_fal_trim_{uuid4().hex[:8]}.mp4")
            cut = subprocess.run(
                [ff, "-y", "-hide_banner", "-ss", f"{start_t:.3f}", "-i", clip,
                 "-t", f"{out_sec:.3f}", "-c:v", "libx264", "-profile:v", "high",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "aac", out],
                capture_output=True,
            )
            if cut.returncode != 0 or not Path(out).exists():
                return clip, dur  # 트림 실패 — 원본 + 실측 길이
            return out, out_sec
        except Exception:
            # 트림은 품질 개선용 best-effort — 어떤 실패도 원본 클립으로 폴백한다.
            return clip, None

    def _stitch(self, clips: list[str], durations: list[float | None] | None = None) -> str:
        """여러 8초 클립을 ffmpeg로 이어붙여 하나의 MP4로 만든다.

        `settings.veo_fal_crossfade_sec`>0 이고 모든 클립 길이를 알면 비트 경계에 짧은
        디졸브(xfade/acrossfade)를 줘 의상·구도 점프를 부드럽게 가린다(2026-06-29 PO
        옵션 B — 근본 제거가 아닌 완화). 길이를 모르거나 디졸브 ffmpeg이 실패하면 단순
        concat으로 안전 폴백한다. 클립 1개면 스티칭 없이 그대로 반환한다. ffmpeg 바이너리는
        imageio-ffmpeg 번들을 쓴다(시스템 설치 불요). 실패(ffmpeg 비정상 종료·미설치)는
        VideoRenderError 계약으로 변환하며, 입력 경로가 박힐 수 있는 stderr 원문은 노출하지
        않고 예외 타입명만 남긴다(redaction).
        """
        if len(clips) == 1:
            return clips[0]
        dissolve = float(getattr(self.settings, "veo_fal_crossfade_sec", 0.0) or 0.0)
        if dissolve > 0 and durations is not None and len(durations) == len(clips):
            faded = self._stitch_dissolve(clips, durations, dissolve)
            if faded is not None:
                return faded
        return self._concat(clips)

    def _stitch_dissolve(
        self, clips: list[str], durations: list[float | None], dissolve: float
    ) -> str | None:
        """클립 경계에 짧은 디졸브(xfade+acrossfade)를 줘 이어붙인다(best-effort).

        모든 클립 길이가 유효하고 디졸브보다 충분히 길 때만 offset 누적이 성립한다 —
        하나라도 길이를 모르거나 너무 짧으면 None을 돌려 호출부가 concat으로 폴백한다.
        디졸브 ffmpeg 실패(필터 비호환·타임아웃 등)도 None으로 안전 폴백. xfade는 입력
        해상도/fps/SAR가 같아야 하므로 각 비디오를 fps/format/SAR로 정규화한 뒤 체이닝한다.
        """
        dur: list[float] = []
        for d in durations:
            # 디졸브보다 충분히 길어야 offset=길이-디졸브가 양수로 성립한다.
            if d is None or d <= dissolve + 0.1:
                return None
            dur.append(float(d))
        import subprocess

        import imageio_ffmpeg

        out_path = Path(self.settings.nutti_media_dir) / f"video_{uuid4().hex[:12]}.mp4"
        inputs: list[str] = []
        for clip in clips:
            inputs += ["-i", clip]
        n = len(clips)
        parts: list[str] = [f"[{i}:v]format=yuv420p,fps=30,setsar=1[v{i}]" for i in range(n)]
        # 비디오 xfade 체인: 클립 k 합류 시 offset = 직전 출력길이 - 디졸브.
        vlabel = "v0"
        cum = dur[0]
        for k in range(1, n):
            offset = cum - dissolve
            out = f"vx{k}"
            parts.append(
                f"[{vlabel}][v{k}]xfade=transition=fade:"
                f"duration={dissolve:.3f}:offset={offset:.3f}[{out}]"
            )
            vlabel = out
            cum = cum + dur[k] - dissolve
        # 오디오 acrossfade 체인: 경계에서 자동으로 끝-시작을 겹쳐 페이드(offset 불요).
        alabel = "0:a"
        for k in range(1, n):
            out = f"ax{k}"
            parts.append(f"[{alabel}][{k}:a]acrossfade=d={dissolve:.3f}[{out}]")
            alabel = out
        cmd = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(parts),
            "-map",
            f"[{vlabel}]",
            "-map",
            f"[{alabel}]",
            # 출력 코덱/픽셀포맷 강제(2026-06-29): fal Veo 원본은 yuv444p(High 4:4:4)라
            # -pix_fmt 미지정 시 출력도 yuv444p가 되어 Windows 기본 플레이어·브라우저가
            # "지원되지 않는 인코딩"으로 거부한다. yuv420p+High 프로파일로 보편 호환 보장,
            # +faststart로 웹 스트리밍 즉시 재생.
            "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError):
            Path(out_path).unlink(missing_ok=True)
            return None  # 디졸브 실패 — 호출부가 concat 폴백
        log.info("video.stitched.dissolve", path=str(out_path), clips=n, dissolve=dissolve)
        return str(out_path)

    def _concat(self, clips: list[str]) -> str:
        """여러 클립을 디졸브 없이 단순 재인코딩 concat으로 이어붙인다(폴백 경로)."""
        import subprocess

        import imageio_ffmpeg

        out_path = Path(self.settings.nutti_media_dir) / f"video_{uuid4().hex[:12]}.mp4"
        inputs: list[str] = []
        for clip in clips:
            inputs += ["-i", clip]
        n = len(clips)
        # concat 필터는 모든 입력의 픽셀포맷/SAR/fps가 같아야 한다 — fal 클립이 섞이면
        # (yuv444p/yuv420p 혼재) 실패하므로 입력마다 yuv420p·30fps·SAR=1로 정규화한다.
        parts: list[str] = [f"[{i}:v]format=yuv420p,fps=30,setsar=1[cv{i}]" for i in range(n)]
        streams = "".join(f"[cv{i}][{i}:a]" for i in range(n))
        parts.append(f"{streams}concat=n={n}:v=1:a=1[v][a]")
        cmd = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(parts),
            "-map",
            "[v]",
            "-map",
            "[a]",
            # 출력 코덱/픽셀포맷 강제(2026-06-29): yuv444p 원본이 그대로 새어 Windows
            # 기본 플레이어·브라우저가 거부하는 것을 막는다(_stitch_dissolve와 동일 처방).
            "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart",
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
