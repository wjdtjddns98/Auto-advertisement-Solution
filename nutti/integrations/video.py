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
    # 비트 클립이 독립 생성돼 끝 자세가 제각각이면 다음 클립과 점프가 생긴다(PO 피드백
    # 2026-06-29). 자세를 처음부터 끝까지 고정하고, 끝을 페이드 없이 또렷한 프레임으로
    # 마무리하게 해 프레임 체이닝(끝 프레임→다음 시작 프레임)이 안정적으로 물리도록 한다.
    _MOTION_HOLD = (
        "The puppy stays in the exact same upright seated position for the entire shot, "
        "sitting still and centered, holding the same pose from the first frame to the "
        "last frame; it does not lie down, stand up, walk, or leave the frame. The clip "
        "ends on a clean, fully-lit, sharp frame with the puppy seated and centered — no "
        "fade-out, no dimming, no blur at the end."
    )
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
        한글 자막을 임의 렌더하면 깨진 글자로 나오기 때문(settings.veo_fal_negative_prompt와 이중 방어).
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
            f"{self._MOTION_HOLD} "
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
        # 각 비트의 시작 프레임. 1번 비트는 마스코트 Kontext 프레임에서 시작하고, 이후
        # 비트는 직전 클립의 끝 안정 프레임으로 이어 붙여 비트 경계 점프를 줄인다.
        current_frame = frame_path
        try:
            for i, beat in enumerate(beats, start=1):
                # 정면 1인 발화(off_screen_interviewer=False) — 인터뷰 마이크 연출 제거
                # (2026-06-16 PO 피드백: 마이크 구도 아예 삭제).
                prompt = builder.build_beat(beat, off_screen_interviewer=False, style=style)
                clip_path = client.generate(current_frame, prompt)
                log.info("video.veo_fal.clip.done", path=clip_path, beat=i, of=len(beats))
                clips.append(clip_path)
                # 가드된 체이닝: 다음 비트가 있으면 이 클립의 끝 안정 프레임을 다음 시작
                # 프레임으로 쓴다. 추출·품질 가드(검정/빈/가로) 실패 시 None → 원본 마스코트
                # 프레임으로 안전 폴백(망가진 프레임이 다음 클립에 누적되지 않게 하는 핵심 가드).
                if i < len(beats):
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
        total = 0.0
        for clip in clips:
            path, sec = self._trim_to_speech(clip)
            trimmed.append(path)
            total += sec if sec is not None else _CLIP_SEC
        # 트림으로 새로 만든 임시 파일(veo_fal_trim_*.mp4)은 스티칭 후 정리한다 — 원본
        # 비트 클립은 기존 정책대로 유지하고, 단일 비트라 _stitch가 그대로 돌려준 파일
        # (final)은 삭제 대상에서 제외한다(반환 파일 삭제 방지). 스티칭 실패 시에도 정리.
        final = None
        try:
            final = self._stitch(trimmed)
            return final, total
        finally:
            for orig, t in zip(clips, trimmed):
                if t != orig and t != final:
                    try:
                        Path(t).unlink(missing_ok=True)
                    except OSError:
                        pass

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
            # 뒤 침묵: 마지막 침묵이 EOF까지 이어지면(=닫히지 않거나 파일 끝 근처에서 닫힘)
            # 그 시작이 발화 끝점. 중간에서 닫힌 침묵(발화 사이 짧은 멈춤)은 트림하지 않는다.
            end_t = dur
            if starts:
                last = starts[-1]
                open_to_eof = len(ends) < len(starts)  # 마지막 침묵이 EOF까지 안 닫힘
                closed_near_eof = bool(ends) and ends[-1] >= dur - 0.1
                if last > start_t and (open_to_eof or closed_near_eof):
                    end_t = min(dur, last + 0.35)
            out_sec = end_t - start_t
            if out_sec < 0.8 or end_t <= start_t:
                return clip, dur  # 과도 트림 방지(전구간 침묵·검출 이상) — 실측 길이 유지
            # 실제로 잘라낼 무음이 0.5초 미만이면 재인코딩하지 않고 원본 유지 — Veo 클립은
            # 룸톤이 끝까지 깔려 데드에어가 거의 없다(2026-06-16 실측). 의미 있는 무음이
            # 있을 때만 트림해 무익한 재인코딩·중복 파일 생성을 막는다.
            if (dur - out_sec) < 0.5:
                return clip, dur  # 트림 안 함 — 실측 길이 유지
            out = str(Path(self.settings.nutti_media_dir) / f"veo_fal_trim_{uuid4().hex[:8]}.mp4")
            cut = subprocess.run(
                [ff, "-y", "-hide_banner", "-ss", f"{start_t:.3f}", "-i", clip,
                 "-t", f"{out_sec:.3f}", "-c:v", "libx264", "-c:a", "aac", out],
                capture_output=True,
            )
            if cut.returncode != 0 or not Path(out).exists():
                return clip, dur  # 트림 실패 — 원본 + 실측 길이
            return out, out_sec
        except Exception:
            # 트림은 품질 개선용 best-effort — 어떤 실패도 원본 클립으로 폴백한다.
            return clip, None

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
