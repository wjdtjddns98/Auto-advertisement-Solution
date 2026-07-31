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

import re
import time
import zlib
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from nutti.config import Settings, _usable_key
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

# 경계 유사도 스티칭(_find_similarity_cuts) 파라미터(2026-07-07 PO 지시). 고정 지점
# 트림 대신 경계 근처 프레임의 이미지 유사도로 자연스러운 컷 지점을 찾는다.
_SIM_FRAME_STEP = 0.1  # 후보 프레임 간격(초)
_SIM_GAP = 0.15  # 발화 끝/시작에서 탐색을 띄우는 여유(초) — 입모양 겹침 회피
_SIM_A_WINDOW = 1.5  # A(왼쪽 클립) 후보 구간 최대 폭(초, speech_end_a 기준)
_SIM_MIN_B_WINDOW = 0.2  # 이 미만이면 B 후보를 t=0 고정 단일 프레임으로 수렴
# 유사도 매칭에 성공한 경계에 쓰는 마이크로 디졸브(초). 유사 프레임 간에는 긴 디졸브가
# 오히려 "멈춤+페이드아웃"으로 보인다(2026-07-10 PO — 비트 끊김 체감의 직접 원인) —
# 사실상 하드컷이되, 오디오 acrossfade 클릭 방지를 위해 0이 아닌 2~3프레임 값을 쓴다.
_SIM_CUT_DISSOLVE = 0.08
_SIM_W, _SIM_H = 64, 114  # 유사도 비교용 저해상도 그레이스케일 프레임 크기(9:16 축소)

# 스티칭 정규화 해상도(9:16 쇼츠). 모든 입력을 이 크기로 맞춰 xfade/concat의 크기 불일치
# 실패를 방지하고, 교차 펀치인 크롭의 기준 좌표계가 된다.
# ponytail: 720x1280 고정(fal Veo Lite 실측) — 모델 해상도를 올리면 이 상수도 함께 올릴 것.
_STITCH_W = 720
_STITCH_H = 1280

# 자막 굽기용 한글 폰트 후보(앞에서부터 존재하는 첫 파일 사용). Windows 맑은고딕 →
# Debian/Ubuntu Noto CJK(fonts-noto-cjk, Dockerfile에 포함) → 나눔고딕 순.
_CAPTION_FONT_CANDIDATES = [
    # 로컬 전용 폰트(2026-07-10 PO 지시 — '여기어때 잘난체', 상업용 무료, noonnu.cc
    # 배포. 영상 렌더 사용은 라이선스상 허용이나 "폰트 파일 배포"는 금지라 이 저장소가
    # public이라 커밋하지 않는다 — .gitignore의 assets/fonts/ 참조). 이 머신에 파일이
    # 있으면(로컬 배치) 최우선 사용, 없으면(CI·새 클론·Docker) 아래 폴백으로 넘어간다.
    # 경로는 이 파일(video.py) 기준 상대경로로 계산해 리포지토리 어디서 실행해도 찾는다.
    str(Path(__file__).resolve().parents[2] / "assets" / "fonts" / "yg-jalnan.otf"),
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
]


def _asciify_font_path(font: str) -> str:
    """non-ASCII 경로의 폰트를 ASCII 임시 경로로 복사해 그 경로를 돌려준다.

    ffmpeg drawtext의 폰트 로딩(freetype)은 Windows에서 UTF-8 경로를 ANSI로 열어
    한글 폴더가 낀 경로의 파일을 못 연다 — 이때 에러 없이 fontconfig 기본 폰트로
    폴백해 한글 자막이 전부 □(tofu)로 굽힌다(실측 2026-07-13, 리포지토리 루트
    "광고 자동화 솔루션" 밑의 yg-jalnan.otf). textfile은 avio 경유라 한글 경로여도
    무관 — 폰트 경로만 우회하면 된다. 복사 실패 시 원 경로 반환(best-effort).
    """
    if font.isascii():
        return font
    import shutil
    import tempfile

    dest = Path(tempfile.gettempdir()) / f"nutti_font_{Path(font).name}"
    if not str(dest).isascii():
        log.warning("video.caption.font_path_nonascii", path=font)
        return font
    try:
        # 항상 복사한다 — 편당 1회뿐이라 스킵 최적화가 불필요하고, 크기-only 비교는
        # 같은 크기의 다른 폰트로 교체 시 스테일 캐시를 조용히 남긴다(리뷰 지적).
        shutil.copyfile(font, dest)
    except OSError:
        log.warning("video.caption.font_copy_failed", path=font)
        return font
    return str(dest)
# 자막을 문장 단위로 순차 표시하기 위한 분리 기준(2026-07-10 PO — 한 줄씩 넘어가는
# 스타일 요청). 문장 종결부호 뒤 공백에서 나눈다 — ai_text._split_into_beats의 문장
# 분리 정규식과 동일 패턴(대본이 비트당 한국어 2문장을 강제하므로 보통 2개로 나뉜다).
_CAPTION_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。…])\s+")
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


def _close_owned(client) -> None:
    """자체 생성한 연동/HTTP 클라이언트를 안전하게 닫는다.

    close()를 가진 실 클라이언트는 그걸 호출하고, close가 없는 대체 구현
    (테스트 monkeypatch가 반환하는 fake 등)은 조용히 건너뛴다.
    """
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _frame_mad(a: bytes, b: bytes) -> float:
    """두 그레이스케일 raw 프레임의 평균절대차(MAD, 픽셀당 0~255)를 계산한다.

    `_find_similarity_cuts`가 경계 후보 프레임 쌍 중 가장 비슷한 쌍을 고르는 데 쓴다.
    """
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


class _HttpClosingMixin:
    """지연 생성한 `httpx.Client`(self._http)를 닫는 close() 제공.

    각 클라이언트는 self._http에 httpx.Client를 지연 캐싱하는데, 닫지 않으면
    장기 실행 스케줄러에서 TCP 연결 풀/파일 디스크립터가 누적된다. 이 믹스인은
    멱등 `close()`를 더해 누수를 막는다. 주입받은 클라이언트도
    소유권이 호출부로 넘어온 것으로 보고 닫는다(호출부는 자체 생성분만 닫음).
    """

    _http = None

    def close(self) -> None:
        _close_owned(self._http)
        self._http = None


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


# ================= 영상 프롬프트 하드가드(2026-07-07 PO 지시) =================
# 대본 파서와 같은 원리 — "프롬프트 관례"로만 지키던 규칙을 과금 전에 코드로 강제한다.
# 실측 렌더 사고 리터럴: "tripod"→화면에 삼각대 렌더(2026-06-29), "Nutti"/"9:16"→화면
# 자막으로 렌더(2026-06-16). PO 수정 구역(의상·장소·연출 템플릿)을 고치다 실수로
# 들어가면 테스트 전에 여기서 잡힌다. 사고 단어가 새로 실측되면 목록에 추가.
_PROMPT_BANNED_LITERALS = ["tripod", "nutti", "누띠", "누티", "9:16"]


def _validate_visual_prompt(prompt: str, *, expected_quotes: int) -> None:
    """조립 완료된 Veo/Kontext 프롬프트의 하드룰 검증 — 위반 시 과금 전에 시끄럽게 실패.

    expected_quotes: ASCII 작은따옴표(') 기대 개수 — 비트 프롬프트는 대사 인용 한 쌍(2),
    프레임 프롬프트는 0. 어긋나면 인용 탈출 주입 방어의 전제가 깨진 것이다.
    """
    low = prompt.lower()
    for word in _PROMPT_BANNED_LITERALS:
        if word in low:
            raise ValueError(
                f"영상 프롬프트 하드룰 위반: 금지 리터럴 '{word}' 포함 — 화면 렌더 사고"
                " 실측 단어입니다. PO 수정 구역(의상·장소·연출) 문구를 확인하세요."
            )
    quotes = prompt.count("'")
    if quotes != expected_quotes:
        raise ValueError(
            f"영상 프롬프트 하드룰 위반: 작은따옴표 {quotes}개(기대 {expected_quotes}) — "
            "템플릿/의상/장소 문구의 ASCII 작은따옴표(주입 방어 충돌)를 제거하세요."
        )


class EpisodeStyle(NamedTuple):
    """편 단위 연출 스타일(의상·장소상황·소품·포맷).

    한 편 안에서는 시작 프레임과 모든 비트 프롬프트가 같은 스타일을 공유해
    시각 일관성을 유지하고, 편이 바뀌면 다른 조합이 나와 채널이 단조롭지 않게 한다.
    `prop`(빈 문자열=소품 없음)·`fmt`("direct"=정면 정보전달 | "interview"=화면 밖
    인터뷰어+마이크 연출)는 2026-07-16 PO 지시(영상 다양성) — 기본값이 있어 기존
    2-필드 생성 코드와 호환된다. `shot`(시작 프레임 구도·표정)은 2026-07-23 연속 편
    중복 방지를 위해 스타일로 승격 — 빈 문자열이면 _frame_prompt가 종전 해시 선택으로
    폴백한다(레거시 생성 코드 호환).
    """

    outfit: str
    setting: str
    prop: str = ""
    fmt: str = "direct"
    shot: str = ""


# ======================= PO 수정 구역 (편별 연출 로테이션) =======================
# 편마다 마스코트의 "옷"·"장소·상황"·"소품"·"포맷"이 바뀐다(2026-06-12 PO 지시 +
# 2026-07-16 소품·포맷 추가 — 매번 다른 옷·소품·연출). 항목을 추가/삭제하면 조합 수가
# 바뀐다(현재 의상5×장소6×소품6×포맷3 = 540 조합). 영어 묘사에 ASCII 작은따옴표(')는 금지 — 비트 프롬프트의
# 대사 인용 구분자와 충돌해 주입 방어 검증이 깨진다(U+2019는 허용).
_EPISODE_OUTFITS = [
    "a tiny yellow raincoat",
    "a cozy cream knitted sweater",
    "a sporty grey hoodie",
    "a light blue denim jacket",
    "a fluffy red scarf with a matching beanie",
]
# 소품 로테이션(2026-07-16 PO — 옷만 바뀌어 단조로움, 모자·머리 위 선글라스 같은 소품
# 추가). 빈 문자열=소품 없음(2/6 확률 — "조금씩" 추가라 매편 소품은 과함). 규칙:
# 반드시 머리·귀 위에 얹는 소품만 — 눈·입을 가리면 표정·립싱크가 죽는다(선글라스는
# 항상 머리 위에 얹은 상태로 명시). ASCII 작은따옴표(') 금지.
_EPISODE_PROPS = [
    "",
    "",
    "a tiny straw sun hat resting on top of its head",
    "cute toy sunglasses perched on top of its head, above the eyes, never covering them",
    "a small red beret tilted playfully to one side of its head",
    "a little daisy flower clip tucked into the fur on its head",
]
# 포맷 로테이션(2026-07-16 PO — 포맷 다양화): 목록·선택 로직은 ai_text의
# EPISODE_FORMATS/pick_episode_format이 단일 소스다 — 대본 구조(vlog/vet 톤)와 영상
# 연출(마이크·수의사 세트)이 같은 포맷을 봐야 하므로 여기서 중복 정의하지 않는다.
# "interview"=화면 밖 인터뷰어+마이크(_MIC), "vet"=수의사 상황극(아래 가운·진료실
# 오버라이드), "vlog"=정면 발화(대본만 다름). 2026-07-20 PO: 3종+전 포맷 반말로 축소.
# 수의사 상황극 전용 의상·장소(2026-07-16 PO). 로테이션 대신 고정 — 콘셉트 유지를 위해
# 소품도 뽑지 않는다(밀짚모자 쓴 수의사는 콘셉트 붕괴). ASCII 작은따옴표(') 금지.
# 흰 가운은 순백 털에 묻혀 Kontext가 통째로 떨궜다(2026-07-20 run11 실측 — 배경만 반영,
# 가운·청진기 누락 → PO 반려). 색 대비(민트 스크럽+검정 청진기)와 "실제 옷 레이어" 명시로
# 시각적으로 강제한다. 데님·스웨터 등 유색 의상은 같은 프롬프트 경로에서 정상 렌더(실측).
_VET_OUTFIT = (
    "a tiny mint-green veterinarian scrub top with a neat folded collar, worn as a clearly "
    "visible clothing layer over its white fur, plus a black stethoscope with a shiny "
    "silver chest piece draped around its neck"
)
_VET_SETTING = "sitting at the examination desk of a bright, tidy veterinary clinic room"
# 전 항목 sitting 계열로 통일(2026-07-06 PO) — standing 시작 프레임이 뽑히면 클립 전체가
# 이족보행 인형탈 느낌이 되고, 모션 지시(_MOTION_HOLD/_MOTION_LIVELY의 "stays seated")와
# 모순돼 드리프트를 유발한다. 새 장소를 추가할 때도 sitting 자세로 쓸 것.
# (2026-07-21 2족보행 전환을 시도했다가 PO 지시로 당일 철회 — 복원 시 PR #117 diff 참조.)
_EPISODE_SETTINGS = [
    "sitting on a busy city sidewalk like a street interview",
    "sitting on a cozy living room sofa under warm lamps",
    "sitting on a park bench on a sunny afternoon",
    "sitting on a bright modern kitchen floor",
    "sitting in front of a cute pet shop entrance",
    "sitting at a tidy home office desk like a news anchor",
]
# 시작 프레임 구도·표정 로테이션(2026-07-20 PO — "썸네일이 전부 같은 자세"): Shorts
# 썸네일은 영상 프레임에서 자동 추출되므로 시작 프레임 구도가 곧 썸네일이다. 종전엔
# "정면 응시·차분한 표정" 한 가지 고정이라 매편 똑같아 보였다. 전 항목 sitting 유지
# (기립 드리프트 가드 보존)·얼굴 정면 가시(립싱크 가독) 범위에서 앵글·표정만 바꾼다.
# FLF 앵커 특성상 영상 전체 구도도 이 프레임을 따라간다. ASCII 작은따옴표(') 금지.
# 2026-07-23 PO "싸가지 먹방" 컨셉: 차분·친근 계열 → 건방·심드렁 계열로 교체(레퍼런스:
# 충주맨 낮은 자세 토크). sitting 유지(기립 드리프트 가드)·얼굴 정면 가시(립싱크 가독)
# 범위에서 태도만 바꾼다. 과장 표정 단어(cheeky/exaggerated)는 얼굴 왜곡 실측이 있어
# unbothered/unimpressed/deadpan/smug 계열의 절제 어휘만 사용 — 라이브 프레임 렌더로 검증.
_FRAME_SHOTS = [
    "sitting back with a relaxed, unbothered slouch, chin slightly raised, giving the "
    "camera a confident, unimpressed look as if mildly annoyed to be filmed",
    "framed in a close-up from the chest up, head tilted back a little with half-lidded, "
    "unimpressed eyes looking down toward the camera in a deadpan way",
    "captured from a slight three-quarter angle, giving the camera a sassy sideways "
    "glance while its face stays clearly visible and turned toward the lens",
    "seen from a slightly low camera angle so the puppy appears to look down on the "
    "viewer, chin up, with a smug, self-assured expression",
    "leaning back lazily as if lounging, one front paw resting near its snack, staring "
    "at the camera with a deadpan, unimpressed face",
]
# ===================== PO 수정 구역 끝 (편별 연출 로테이션) =====================


def _pick_rotation(salt: str, key: str, options: list[str], avoid: str = "") -> str:
    """CRC32 결정 선택 + 직전 편 값 회피(같으면 다음 인덱스 — pick_episode_format과 동일 패턴).

    avoid가 빈 문자열이면 회피하지 않는다 — 소품 로테이션의 "소품 없음"("")은 연속돼도
    자연스러우므로 회피 대상이 아니다.
    """
    idx = zlib.crc32(f"{salt}:{key}".encode()) % len(options)
    if avoid and options[idx] == avoid:
        idx = (idx + 1) % len(options)
    return options[idx]


def pick_episode_style(
    script_id: str,
    topic: str | None = None,
    fmt: str | None = None,
    avoid: dict[str, str] | None = None,
) -> EpisodeStyle:
    """script.id의 CRC32로 의상·장소·소품·구도를, 주제 해시로 포맷을 결정적으로 고른다.

    각 축은 서로 다른 salt로 해시해 독립적으로 조합된다 — 같은 salt를 쓰면
    리스트 길이가 같을 때 인덱스가 동기화돼 조합 다양성이 리스트 길이로 줄어든다.
    fmt가 오면(오케스트레이터가 직전 편 회피를 반영해 확정한 Script.episode_format)
    그걸 그대로 쓴다 — 해시 재계산으로는 회피 결과를 복원할 수 없기 때문. 없으면
    주제 해시, topic도 없으면(레거시 호출·테스트) script_id 폴백(결정성 유지).
    "vet" 포맷은 의상·장소를 수의사 세트로 고정하고 소품을 뽑지 않는다(콘셉트 보호) —
    구도(shot)만 로테이션해 vet 편끼리도 썸네일이 달라지게 한다.

    avoid(직전 게시 편의 축별 사용값, state 저장분)와 같게 나오면 그 축만 다음
    인덱스로 민다 — 연속 편 시각 중복 방지(2026-07-23 PO "영상 중복도"). 포맷 축의
    PR #117과 같은 계약: 저장은 업로드 성공 시에만(오케스트레이터).
    """
    from nutti.integrations.ai_text import pick_episode_format

    avoid = avoid or {}
    fmt = fmt or pick_episode_format(topic if topic is not None else script_id)
    shot = _pick_rotation("shot", script_id, _FRAME_SHOTS, avoid.get("shot", ""))
    if fmt == "vet":
        return EpisodeStyle(_VET_OUTFIT, _VET_SETTING, "", fmt, shot)
    return EpisodeStyle(
        _pick_rotation("outfit", script_id, _EPISODE_OUTFITS, avoid.get("outfit", "")),
        _pick_rotation("setting", script_id, _EPISODE_SETTINGS, avoid.get("setting", "")),
        _pick_rotation("prop", script_id, _EPISODE_PROPS, avoid.get("prop", "")),
        fmt,
        shot,
    )


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
    # 2026-07-23 PO "싸가지 먹방" 컨셉(레퍼런스: 충주맨 낮은 자세 토크): 차분·친근 →
    # 건방·심드렁. 단 cheeky/exaggerated 같은 과장 표정 단어는 얼굴 왜곡 실측이 있어
    # 쓰지 않는다 — unbothered/confident/deadpan 계열의 절제된 어휘로만 태도를 만든다.
    _PERSONA = (
        f"{_MASCOT_APPEARANCE}, confident, unbothered and nonchalant, talking to the "
        "camera in a relaxed, self-assured way like it slightly looks down on the "
        "viewer, with subtle, natural, deadpan facial expressions and no "
        "exaggerated or distorted faces"
    )
    _VOICE = (
        "Voice (must be EXACTLY the same single voice in every clip of this series, like "
        "one specific recognizable person with a fixed vocal fingerprint): a bright, "
        "cute Little girl Korean voice, sounding about 6 years old, slightly high-pitched, "
        "cheeky and energetic, with a warm soft timbre and a consistent speaking rhythm at "
        "a lively natural pace. "
        # 발음 교정(2026-07-06 PO 실측: 쉬운 단어도 발음이 뭉개짐 — 아이 페르소나의
        # 혀 짧은 딕션 재현이 유력 원인). 톤은 아이답게 유지하되 발음만 성인급 정확도로.
        "Her Korean PRONUNCIATION however is flawlessly clear and precise: perfect "
        "standard Korean diction, every syllable fully and accurately articulated, "
        "never slurred, never mumbled, never babyish or lisping — like a professional "
        "child voice actor whose enunciation is adult-level crisp and correct. "
        "Keep the identical timbre, pitch, accent, and speaking speed "
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
    # 앉은 채 자연스러운 제스처를 허용해 생기를 준다. 단 화면 이탈·기립·눕기는 막고
    # 끝 페이드는 금지한다(negative_prompt 억제와 이중 방어).
    # 2026-07-10 PO("비트별로 페이드아웃되는 기분"): 종전의 "끝 2~3초 진정(wind-down)"
    # 강제를 제거 — 매 비트 끝마다 에너지가 죽어 페이드아웃처럼 보이는 직접 원인이었다.
    # 끝 포즈 수렴은 FLF 모델이 물리적으로 담당하므로 프롬프트 진정 지시는 불필요한
    # 이중 방어였다(수렴 실패는 QC의 tail_not_converged가 잡는다).
    # 2026-07-16 PO("캐릭터가 너무 정적이라 밋밋함"): 중간 비트의 제스처 어휘를
    # _MOTION_FINAL_FREE에서 이미 검증된 수준(앞발 흔들기·귀 쫑긋·꼬리 흔들기·상체
    # 리액션)으로 확대. 화면 이탈·기립·끝 페이드 가드와 FLF 끝 포즈 수렴은 그대로 유지.
    # 2026-07-20 PO("팔을 너무 자주 흔듦, 자연스러운 움직임 필요"): 앞발 제스처를
    # "클립당 최대 1회, 반복 금지"로 제한하고 고개·귀·꼬리·무게 이동·표정 중심으로 전환
    # (_MOTION_FINAL_FREE 동일). 어휘 목록 앞쪽의 paw waves를 Veo가 과도 샘플링한 부작용.
    _MOTION_LIVELY = (
        "The puppy stays seated and centered in frame the whole time but moves naturally "
        "and expressively as it talks — gentle head tilts, small ear twitches, a joyful "
        "tail wag, subtle shifts of body weight, leaning slightly toward the camera, and "
        "lively facial expressions that bring real energy and charm to the shot. Its "
        "front paws stay relaxed on the ground almost the entire time — at most one "
        "brief, small paw gesture in the whole clip, never repeated or constant paw "
        "waving. It is already "
        "in lively motion from the very first moments of the clip — it starts talking and "
        "moving right away, with no still, frozen, or slow warm-up intro. It never "
        "stands up, walks, lies down, hunches over, ducks its head down, curls forward, or "
        "leaves the frame. Keep this natural lively energy all the way to the end of the "
        "clip — do not wind down, slow down, go still, or freeze near the end. The clip "
        "ends on a clean, fully-lit, razor-sharp frame — no fade-out, no dimming, no blur, "
        "no warping, no morphing, no freeze, and no glitch at the end."
    )
    # 마지막 비트(CTA) 전용 모션 — 진정(wind-down) 강제 없이 귀여운 행동을 자유롭게
    # 허용한다(2026-07-06 PO: "마지막 비트는 제한 걸지 말고 귀여운 행동 하게 냅둬").
    # 마지막 비트는 뒤에 이어붙일 클립이 없어 끝 포즈 수렴이 불필요 — 화면 이탈·끝
    # 페이드/글리치 같은 깨짐 방지 최소 가드만 남긴다.
    _MOTION_FINAL_FREE = (
        "The puppy stays seated and centered in frame but is free to be playful and "
        "adorable as it talks — happy head tilts, excited ear wiggles, a joyful tail "
        "wag, cute expressive reactions; at most one brief, small paw gesture, never "
        "repeated or constant paw waving. Let its natural charm show; no "
        "forced calm-down at the end. It never leaves the frame. The clip ends on a "
        "clean, fully-lit, sharp frame — no fade-out, no dimming, no blur, no warping, "
        "and no glitch at the end."
    )
    # 립싱크 강제 — 간헐적으로 입을 안 움직이며 내레이션처럼 나오는 클립 방지
    # (2026-07-06 PO 실측). 모든 비트 프롬프트에 포함.
    _LIPSYNC = (
        "The puppy visibly speaks every word on camera: its mouth clearly opens and moves "
        "in sync with the spoken Korean line from the first word to the last. The voice is "
        "never detached narration or voice-over — it always comes from the puppy talking "
        "on screen with matching mouth movements."
    )
    _NEGATIVE = (
        "The subject is a real live photorealistic puppy — never a mascot suit, fursuit, "
        "costume, person in a costume, or plush toy. Strictly no additional animals, no "
        "people. Absolutely no text, subtitles, captions, letters, numbers, words, logos, "
        "brand names, watermarks, or UI overlays anywhere in the frame."
    )
    # 먹방 연출(2026-07-23 PO): 간식 그릇을 앞에 두고, 클립 "시작"에 한 입만 먹고
    # 말한다. 시작에 두는 이유 — 끝 잉여 트림(_generate_and_trim_clip)이 발화 이후
    # 구간을 잘라내므로 발화 뒤 베어무는 액션은 잘려나간다. 발화 중 씹기는 립싱크
    # 붕괴라 금지, 클립당 한 입 제한(팔흔들기 1회 제한과 같은 과도 샘플링 방어).
    # ponytail: 씹는 소리(ASMR)는 _VOICE의 무음 정책·발화 끝 트림 로직과 충돌해 비주얼만
    # — 소리까지 원하면 트림 로직 개편이 선행돼야 한다.
    # ⚠️ 이 "집어 먹는" 실제 동작은 endframe lock과 공존할 수 없다 — lock은 시작·끝을 같은
    # '안 먹는' 앵커 프레임으로 묶으므로, 중간에 먹으면 끝에서 원상복구를 해야 한다. 그래서
    # Veo가 **입에서 그릇으로 도로 내려놓는 역방향 동작**을 만든다(2026-07-31 PO 실측:
    # "주워서 먹는게 아니라 입에서 그릇으로 내려간다"). food가 있으면
    # _produce_clips_veo_fal이 lock을 끄고 체이닝으로 돌린다(끝 안정 프레임을 다음 비트
    # 시작 프레임으로). 아래 "food never appears out of thin air"는 같은 환각의 이중 방어다.
    _EATING_TEMPLATE = (
        "A small snack bowl with {food} sits right in front of the puppy. At the very "
        "start of the clip, the puppy uses one front paw to pick up a single piece of "
        "the snack from the bowl and bring it up to its mouth, calmly eats that one "
        "piece, and then starts speaking. The snack it eats is always the food already "
        "in the bowl — food never appears out of thin air or drops in from off-screen. "
        "Only this one piece is eaten in the whole clip; once it is speaking it is no "
        "longer chewing or holding food, and the food never blocks or covers its face."
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

    def build_beat(
        self,
        dialogue_text: str,
        *,
        off_screen_interviewer: bool = True,
        style: EpisodeStyle | None = None,
        motion_release: bool = False,
        final_cta: bool = False,
        food: str = "",
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
        `food`(영어 시각 묘사구, Script.food_visual)가 오면 간식 그릇+클립 시작 한 입
        먹방 연출(_EATING_TEMPLATE)이 붙는다 — 비면 기존 연출 그대로(하위호환).
        """
        dialogue = _sanitize_prompt_text(dialogue_text.strip() or "", _MAX_DIALOGUE_CHARS)
        speaking = self._SPEAKING_OFF if off_screen_interviewer else self._SPEAKING_DIRECT
        scene = ""
        if style is not None:
            prop = f", with {style.prop}" if style.prop else ""
            scene = f"The puppy wears {style.outfit}{prop}, {style.setting}. "
        if food:
            scene += self._EATING_TEMPLATE.format(food=food) + " "
        mic = f"{self._MIC} " if off_screen_interviewer else ""
        # 마지막 비트는 진정 강제 없이 귀여운 행동 자유(_MOTION_FINAL_FREE, 2026-07-06 PO) —
        # 뒤에 이어붙일 클립이 없어 끝 포즈 수렴이 필요 없다. 중간 비트는 기존 로직 유지.
        if motion_release and final_cta:
            motion = self._MOTION_FINAL_FREE
        elif motion_release:
            motion = self._MOTION_LIVELY
        else:
            motion = self._MOTION_HOLD
        cta = f"{self._CTA_VOICE_ANCHOR} " if final_cta else ""
        prompt = (
            f"A photorealistic shot of {self._PERSONA}, {speaking}, "
            f"saying (as spoken audio only, no on-screen text): '{dialogue}'. "
            f"{scene}{mic}"
            f"{self._VOICE} {cta}"
            f"{self._LIPSYNC} "
            f"{self._CAMERA} "
            f"{motion} "
            f"{self._CONTINUITY} "
            f"{_CINEMATIC_LOOK} "
            "Format: tall vertical portrait orientation, single continuous 8-second shot. "
            f"{self._NEGATIVE}"
        )
        # 하드가드: 금지 리터럴·인용 구분자 한 쌍 — 과금 전 검증(2026-07-07 PO).
        # 대사(quoted)는 검사에서 제외한다: 음성으로 발화될 뿐 화면 렌더 사고와 무관하고,
        # 대사 속 브랜드명·발음 리스크는 대본 파서(ai_text.validate_script_body) 담당.
        _validate_visual_prompt(prompt.replace(dialogue, ""), expected_quotes=2)
        return prompt


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
        text_judge=None,
    ):
        # 실연동 클라이언트는 주입 가능하게 받는다(테스트에서 fake 주입 → 네트워크 불요).
        # 주입이 없으면 각 실 경로(non-dry_run)에서 지연 생성한다.
        self.settings = settings
        self._nano_client = nano_client
        # fal.ai Veo 3.1 백엔드(veo_fal)용 주입 클라이언트.
        # 미주입 시 _produce_clips_veo_fal에서 지연 생성하고 finally에서 1회 닫는다.
        self._veo_fal_client = veo_fal_client
        # 폴링 대기용 sleep 주입(기본 time.sleep). 테스트에서 가짜 시계로 대체.
        self._sleep = sleep
        # 화면 텍스트 QC 판정자 주입(테스트용): callable(list[프레임 PNG 경로]) ->
        # True(텍스트 있음)|False(없음)|None(판단 보류). 미주입 시 실 경로에서
        # AITextClient.judge_frames_have_text로 지연 생성한다(_judge_frames_text).
        self._text_judge = text_judge

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

    def produce(
        self, script: Script, style_avoid: dict[str, str] | None = None
    ) -> VideoAsset:
        """시작 프레임 → 비트별 fal.ai Veo 클립 → 스티칭 → VideoAsset 반환.

        veo_fal 경로: 대본 비트(`script.beats`)가 N개면 같은 시작 프레임에서 비트마다
        8초 클립을 만들어 ffmpeg로 이어붙인다. 비트가 없으면 body 단일컷(8초)으로 폴백한다.
        실 경로의 정확한 길이는 _produce_clips_veo_fal이 돌려준 값(트림 실측)으로 덮어쓰고,
        아래 duration은 dry_run·사전 추정용 계산이다(비트당 8초 가정).

        style_avoid: 직전 게시 편의 시각 축 사용값(오케스트레이터가 state에서 읽어 전달)
        — 연속 편 의상·장소·구도 중복 방지. None이면 회피 없이 종전 해시 선택과 동일.
        """
        # 실 경로면 시작 전에 필수 키를 검증(미설정 시 빠르게 실패).
        self.validate_config()
        beats = self._beats(script)
        # 비트당 8초 클립을 만들어 스티칭한다(실측 길이는 _produce_clips_veo_fal이 덮어쓴다).
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
        style = pick_episode_style(
            script.id, script.topic, fmt=script.episode_format or None, avoid=style_avoid
        )
        frame_path = self._generate_frame(script, style)
        # 실 경로의 총길이는 위 사전 추정 대신 veo_fal이 돌려준 실측값(비트 클립 앞뒤
        # 침묵 트림 반영)으로 덮어쓴다.
        video_path, duration = self._produce_clips_veo_fal(
            frame_path, beats, style, food=script.food_visual
        )
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

    def _produce_clips_veo_fal(
        self, frame_path: str, beats: list[str], style: EpisodeStyle, food: str = ""
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
        # 먹방(food 있음): 앞발로 간식을 집어 입에 넣는 실제 동작이 필요한데, 이 동작은
        # endframe lock과 충돌한다 — lock이 시작·끝을 같은 '안 먹는' 앵커로 묶으면 Veo가
        # 먹은 상태로 끝낼 수 없어 **입에서 그릇으로 도로 내려놓는 역방향 동작**을 만든다
        # (2026-07-31 PO 실측). 그래서 먹방은 lock을 끄고 체이닝(끝 안정 프레임 → 다음 시작
        # 프레임)으로 경계를 잇고, 정적(_MOTION_HOLD) 대신 생동 모션을 준다
        # (motion_release=True: '한 번의 앞발 제스처'가 곧 집어 먹기).
        # food 없는 편(휴면 포맷 폴백)은 기존 lock 동작 그대로(하위호환).
        # ponytail: 체이닝 경계는 lock보다 매끄러움이 덜할 수 있다 — 유사도 스티칭
        # (_find_similarity_cuts)이 완화하고, 안 맞는 경계만 디졸브 2배로 가린다.
        eating = bool(food)
        use_lock = lock and not eating
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
                # 포맷 로테이션(2026-07-16 PO): "interview" 편은 화면 밖 인터뷰어+마이크
                # 연출(_MIC), "direct" 편은 정면 1인 발화. (2026-06-16의 마이크 전면 삭제를
                # KR "AI 강아지 인터뷰" 유행에 맞춰 로테이션으로 부활.)
                # lock 모드는 끝 프레임이 모델로 고정되므로 모션 제약을 풀어(_MOTION_LIVELY)
                # 생동감을 준다(2026-06-29 PO). 기본 image-to-video 경로는 _MOTION_HOLD 유지.
                # 먹방은 첫 비트에서만 집어 먹는다. 2번 비트부터 food 텍스트를 빼지 않으면
                # 매 비트가 "간식 가득한 그릇"을 다시 렌더해 먹은 간식이 도로 차오른다
                # (PO 실측). 그릇의 시각적 연속성은 체이닝된 끝 프레임이 잇는다.
                beat_food = food if i == 1 else ""
                prompt = builder.build_beat(
                    beat,
                    off_screen_interviewer=(style.fmt == "interview"),
                    style=style,
                    motion_release=use_lock or eating,
                    final_cta=(i == len(beats)),
                    food=beat_food,
                )
                # 생성 + 끝 잉여 고정 트림(글리치 온상 제거, 8초→약7초, 2026-06-29 PO).
                # QC 재생성이 같은 단계를 다시 밟도록 헬퍼로 묶었다.
                clip_path = self._generate_and_trim_clip(
                    client, prompt, current_frame, frame_path, use_lock, video_seed
                )
                # 클립 QC 레이어(2026-07-07 PO): 중간 프리즈·블랙·무발화·꼬리 미수렴을
                # 잡아 그 비트만 재생성한다. 상한(qc_max_retries) 초과 시 현행 트림·마스킹
                # 폴백으로 그대로 수용한다 — 여기서 예외/실패로 파이프라인을 죽이지 않는다.
                # 마지막 비트는 꼬리 수렴 검사 면제(final_beat) — 뒤에 이어붙일 클립이
                # 없고 모션도 자유(_MOTION_FINAL_FREE)라 수렴 실패가 결함이 아니다.
                # 6차 런 실측: 마지막 비트가 tail_not_converged로 2회 재생성($0.8 낭비).
                final_beat = i == len(beats)
                reasons = self._qc_check_beat(
                    clip_path, frame_path, use_lock, final_beat=final_beat
                )
                attempt = 0
                while reasons and attempt < self.settings.qc_max_retries:
                    attempt += 1
                    log.info(
                        "video.veo_fal.qc.retry", beat=i, attempt=attempt, reasons=reasons
                    )
                    Path(clip_path).unlink(missing_ok=True)
                    # 재생성 seed는 오프셋을 준다 — 같은 seed+같은 프롬프트 재제출은 같은
                    # 결함(텍스트 오버레이 등)을 그대로 재현할 수 있어 재시도가 무효가 된다.
                    # 음색 seed 일관성보다 결함 제거가 우선(2026-07-10, 텍스트 QC와 함께).
                    retry_seed = (video_seed + attempt) % (2**31)
                    clip_path = self._generate_and_trim_clip(
                        client, prompt, current_frame, frame_path, use_lock, retry_seed
                    )
                    reasons = self._qc_check_beat(
                        clip_path, frame_path, use_lock, final_beat=final_beat
                    )
                if reasons:
                    log.info("video.veo_fal.qc.fallback", beat=i, reasons=reasons)
                log.info("video.veo_fal.clip.done", path=clip_path, beat=i, of=len(beats))
                clips.append(clip_path)
                # 가드된 체이닝(기본 모드만): 다음 비트가 있으면 이 클립의 끝 안정 프레임을
                # 다음 시작 프레임으로 쓴다. 추출·품질 가드(검정/빈/가로) 실패 시 None → 원본
                # 마스코트 프레임으로 안전 폴백(망가진 프레임이 다음 클립에 누적되지 않게 하는
                # 핵심 가드). lock 모드는 끝프레임을 frame_path로 고정하므로 체이닝하지 않는다.
                if not use_lock and i < len(beats):
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

        # 경계 유사도 스티칭(2026-07-07 PO): 고정 지점 트림 대신 경계 근처 프레임 쌍의
        # 이미지 유사도로 가장 자연스러운 컷 지점을 찾는다. A(왼쪽 클립)는 tail-trim만
        # 된 원본 clips[i]에서 발화 끝(durations[i]) 이후를 탐색한다 — 이미 발화 끝으로
        # 잘린 trimmed[i]는 탐색할 여유가 거의 없다. B(오른쪽 클립)의 발화 시작은 별도로
        # 검출하지 않는다 — 현재 트림은 앞을 자르지 않으므로(_trim_to_speech의 start_t=0
        # 정책) B는 항상 t=0 근방에서 시작하고, endframe_lock 모드에서는 그 t=0 프레임이
        # 모든 클립이 공유하는 마스코트 고정 프레임이라 A의 수렴 프레임과 자연히 유사하다.
        # speech_start_b=0.0을 넘겨 B 후보를 t=0 단일 프레임으로 수렴시킨다(스펙의
        # "0.2s 미만 → 0.0 고정" 분기).
        n_clips = len(clips)
        head_start = [0.0] * n_clips
        tail_end: list[float | None] = list(durations)
        tail_from_sim = [False] * n_clips
        head_from_sim = [False] * n_clips
        dissolve_base = float(getattr(self.settings, "veo_fal_crossfade_sec", 0.0) or 0.0)
        boundary_dissolves = [dissolve_base] * max(0, n_clips - 1)
        any_custom = False
        threshold = float(getattr(self.settings, "stitch_sim_threshold", 0.0) or 0.0)
        if threshold > 0:
            for i in range(n_clips - 1):
                speech_end_a = durations[i]
                if speech_end_a is None:
                    continue  # 발화 끝 미상 — 유사도 탐색 불가, 기존 트림 유지
                try:
                    result = self._find_similarity_cuts(
                        clips[i], clips[i + 1], speech_end_a, 0.0, threshold=threshold
                    )
                except Exception:
                    result = None
                if result is None:
                    # 관측 로그: 이 경계는 매칭 시도조차 못 해 기본 디졸브로 폴백된다 —
                    # 라이브 런에서 "왜 이 경계만 페이드처럼 보이나"를 추적하는 신호.
                    log.info("stitch.sim_search_miss", boundary=i)
                    continue  # ffmpeg 실패·프레임 부족 등 — best-effort 폴백(기존 트림 유지)
                cut_a, cut_b, diff = result
                if diff <= threshold:
                    tail_end[i] = cut_a
                    tail_from_sim[i] = True
                    head_start[i + 1] = cut_b
                    head_from_sim[i + 1] = True
                    # 관측 로그: 발화 끝에서 컷까지의 잔여 초 — 설틀 꼬리가 실제로
                    # 잘리는지 라이브 런 로그로 확인하는 유일한 신호(2026-07-10).
                    log.info(
                        "stitch.sim_cut",
                        boundary=i,
                        tail_sec=round(cut_a - speech_end_a, 2),
                        diff=round(diff, 1),
                    )
                    # 유사 프레임끼리는 디졸브 대신 사실상 하드컷(마이크로 디졸브)으로
                    # 붙인다 — 거의 같은 두 정지 프레임을 0.35초 디졸브하면 그 구간이
                    # "멈춤+페이드아웃"으로 보이는 것이 비트 끊김 체감의 직접 원인
                    # (2026-07-10 PO). 스트레이트 컷은 연속 동작으로 읽힌다. 오디오
                    # acrossfade 클릭 방지를 위해 0이 아닌 마이크로 값(0.08s)을 쓴다.
                    boundary_dissolves[i] = min(dissolve_base, _SIM_CUT_DISSOLVE)
                    any_custom = True
                else:
                    log.warning("stitch.boundary_mismatch", diff=diff, pair=(i, i + 1))
                    boundary_dissolves[i] = dissolve_base * 2
                    any_custom = True

        # 유사도 컷이 결정된 클립만 clips[k](원본)에서 [head_start, tail_end)로 다시
        # 잘라낸다 — trimmed[k]는 그대로 두고 새 파일로 교체해 미개입 클립은 기존
        # _trim_to_speech 산출물을 그대로 재사용한다(threshold<=0이면 아예 무개입).
        final_trimmed = list(trimmed)
        final_durations: list[float | None] = list(durations)
        sim_cut_files: list[str] = []
        for k in range(n_clips):
            if not (tail_from_sim[k] or head_from_sim[k]):
                continue
            if durations[k] is None:
                continue  # 발화 끝 미상 클립은 병합 컷을 보류하고 기존 트림 유지
            end_k = tail_end[k] if tail_from_sim[k] else durations[k]
            start_k = head_start[k] if head_from_sim[k] else 0.0
            cut_path = self._cut_clip_range(clips[k], start_k, end_k)
            if cut_path is None:
                continue  # 컷 실패 — 기존 트림 유지(best-effort)
            final_trimmed[k] = cut_path
            final_durations[k] = end_k - start_k
            sim_cut_files.append(cut_path)

        # 트림으로 새로 만든 임시 파일(veo_fal_trim_*.mp4/veo_fal_simcut_*.mp4)은 스티칭
        # 후 정리한다 — 원본 비트 클립은 기존 정책대로 유지하고, 단일 비트라 _stitch가
        # 그대로 돌려준 파일(final)은 삭제 대상에서 제외한다(반환 파일 삭제 방지).
        # 스티칭 실패 시에도 정리.
        final = None
        try:
            if any_custom:
                final = self._stitch(
                    final_trimmed, final_durations, boundary_dissolves=boundary_dissolves
                )
            else:
                final = self._stitch(final_trimmed, final_durations)
            # 자막 굽기(2026-07-06 PO): 비트별 대사를 하단 한글 자막으로. best-effort —
            # 실패/폰트 없음이면 무자막 원본 유지. 성공 시 자막 전 스티칭 산출물(중간물)은
            # 삭제하되, 단일 비트처럼 _stitch가 입력을 그대로 돌려준 경우는 남긴다.
            if self.settings.caption_burn:
                captioned = self._burn_captions(
                    final, beats, final_durations,
                    dissolve=getattr(self, "_last_stitch_dissolve", 0.0),
                    boundary_dissolves=getattr(self, "_last_boundary_dissolves", None),
                )
                if captioned is not None:
                    if final not in final_trimmed and final not in clips:
                        try:
                            # missing_ok는 '없음'만 삼킨다 — Windows에서 ffmpeg 핸들
                            # 지연 해제로 PermissionError가 나면 성공한 자막 영상을
                            # 버리게 되므로 OSError 전체를 방어한다(리뷰 지적).
                            Path(final).unlink(missing_ok=True)
                        except OSError:
                            pass
                    final = captioned
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
            for t in sim_cut_files:
                if t != final:
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
            # 앞은 트림하지 않는다(start_t=0). 발화 시작점을 -24dB 윈도로 잡으면 소프트한
            # 첫 음절 온셋(2026-06-30 PO 실측 -25~-28dB)을 발화로 못 보고 잘라 "첫 대사가
            # 살짝 깨지는" 현상이 생긴다(PO 피드백). 앞 ~0.4초는 짧은 룸톤/온셋이라 그대로
            # 둬도 무해하고, 의미 있는 트림은 끝 글리치 구간(아래)뿐이다. 발화 자체가 없으면 폴백.
            if not any(v > _TRIM_SPEECH_MIN for v in env):
                return clip, dur
            start_t = 0.0
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

    def _probe_duration_sec(self, clip: str) -> float | None:
        """ffmpeg -i의 stderr에서 `Duration:` 라인을 파싱해 초 단위 길이를 반환한다.

        `_trim_tail_fixed`의 동일 파싱 로직과 같은 정규식을 쓰되, 유사도 스티칭 전용
        경로(best-effort — 실패 시 None)로 별도 둔다.
        """
        import re
        import subprocess

        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        probe = subprocess.run([ff, "-hide_banner", "-i", clip], capture_output=True)
        err = (probe.stderr or b"").decode("utf-8", "replace")
        dm = re.search(r"Duration:\s*(\d+):(\d+):([0-9.]+)", err)
        if dm is None:
            return None
        return int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3))

    def _extract_gray_frames(self, clip: str, start: float, duration: float) -> list[bytes]:
        """clip의 [start, start+duration) 구간에서 `_SIM_FRAME_STEP` 간격 저해상도
        그레이스케일 프레임을 한 번의 ffmpeg 호출로 뽑아 raw 바이트 리스트로 반환한다.

        프레임마다 subprocess를 띄우지 않고, `fps` 필터로 구간 전체를 한 번에 뽑은 뒤
        파이썬에서 프레임 크기(`_SIM_W`×`_SIM_H`)로 잘라 나눈다. 실패 시 빈 리스트.
        """
        import subprocess

        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        fps = 1.0 / _SIM_FRAME_STEP
        cmd = [
            ff, "-hide_banner", "-ss", f"{start:.3f}", "-i", clip,
            "-t", f"{max(duration, _SIM_FRAME_STEP / 2):.3f}",
            "-vf", f"fps={fps:.3f},scale={_SIM_W}:{_SIM_H},format=gray",
            "-f", "rawvideo", "-",
        ]
        res = subprocess.run(cmd, capture_output=True)
        raw = res.stdout or b""
        frame_size = _SIM_W * _SIM_H
        if len(raw) < frame_size:
            return []
        n = len(raw) // frame_size
        return [raw[k * frame_size:(k + 1) * frame_size] for k in range(n)]

    def _find_similarity_cuts(
        self,
        clip_a: str,
        clip_b: str,
        speech_end_a: float,
        speech_start_b: float,
        threshold: float | None = None,
    ) -> tuple[float, float, float] | None:
        """경계 A(꼬리)·B(머리) 후보 구간에서 이어붙일 프레임 쌍의 컷 지점을 찾는다.

        끝프레임 고정(endframe_lock) 모드는 모든 클립이 같은 마스코트 프레임으로 수렴
        하므로, A의 발화 끝 직후 구간과 B의 발화 시작 직전 구간에는 실제로 유사한 프레임
        쌍이 존재한다. `_SIM_FRAME_STEP` 간격 그레이스케일 프레임 쌍의 평균절대차(MAD,
        0~255)를 계산한다.

        선택 규칙(2026-07-10 PO "매 비트 끝 페이드아웃"): `threshold`가 주어지면 MAD가
        임계 이하인 **가장 이른** A 프레임에서 컷한다 — 최솟값(가장 유사=가장 정지된
        프레임)을 고르면 발화 후 강아지가 고정 끝프레임으로 수렴하며 모션이 죽어가는
        설틀(진정) 꼬리(실측 0.5~1초)를 매 비트 끝에 도로 포함시켜 페이드아웃처럼
        보인다. 임계를 만족하는 프레임이 없으면 전 쌍 최솟값으로 폴백한다(호출부가
        임계 초과=불일치로 판정해 디졸브 2배 마스킹). threshold=None이면 종전 그대로
        전 쌍 최솟값.

        클램프 모드: 대사가 클립 끝까지 차 발화 끝 이후 창이 없으면 클립 마지막
        3프레임으로 창을 옮기고 **가장 늦은** 임계 이하 프레임을 채택한다(대사 잘림
        최소화 — 창 자체가 대사 구간 위에 있기 때문. FLF 고정 끝프레임이라 마지막
        프레임이 B 시작과 유사).

        반환은 (A 컷 시각초, B 컷 시각초, 채택 쌍 MAD)이고, ffmpeg 실패·프레임 부족·
        길이 확인 실패 등 어떤 이유로든 탐색이 불가하면 None을 돌려준다(호출부가 기존
        트림으로 best-effort 폴백).
        """
        try:
            a_start = speech_end_a + _SIM_GAP
            a_dur = self._probe_duration_sec(clip_a)
            if a_dur is None:
                return None
            a_end = min(speech_end_a + _SIM_A_WINDOW, a_dur)
            # 대사가 클립 끝까지 꽉 차면(무음 미검출 → speech_end≈dur) 발화 끝 이후
            # 탐색 창이 소멸한다 — 6차 런 실측: 경계 0·1이 이걸로 매칭을 포기하고 0.35s
            # 디졸브 폴백(페이드 체감), 창이 생긴 경계 2만 하드컷(PO 호평). 포기 대신
            # 클립 마지막 3프레임으로 창을 클램프한다: FLF 고정 끝프레임이라 마지막
            # 프레임은 B 시작(같은 마스코트 프레임)과 유사할 확률이 높다. 이 모드에선
            # 대사 잘림을 최소화해야 하므로 "가장 이른"이 아니라 "가장 늦은" 프레임을
            # 채택한다(아래 clamped 역순 순회).
            clamped = False
            if a_end - a_start < _SIM_FRAME_STEP:
                clamped = True
                a_start = max(0.0, a_dur - 3 * _SIM_FRAME_STEP)
                a_end = a_dur
                if a_end - a_start < _SIM_FRAME_STEP:
                    return None
            a_frames = self._extract_gray_frames(clip_a, a_start, a_end - a_start)
            if not a_frames:
                return None

            b_window = max(0.0, speech_start_b - _SIM_GAP)
            if b_window < _SIM_MIN_B_WINDOW:
                b_frames = self._extract_gray_frames(clip_b, 0.0, _SIM_FRAME_STEP / 2)
                b_frames = b_frames[:1]
            else:
                b_frames = self._extract_gray_frames(clip_b, 0.0, b_window)
            if not b_frames:
                return None
            b_offsets = [k * _SIM_FRAME_STEP for k in range(len(b_frames))]

            best: tuple[float, float, float] | None = None
            # 기본: 시간순(가장 이른 컷 — 설틀 꼬리 제거). 클램프 모드: 역순(가장 늦은
            # 컷 — 창이 대사 위라 뒤로 갈수록 대사가 덜 잘린다).
            order = reversed(list(enumerate(a_frames))) if clamped else enumerate(a_frames)
            for i, fa in order:
                cut_a = a_start + i * _SIM_FRAME_STEP
                # 이 A 프레임의 최적 B 짝을 찾고, 임계 이하면 즉시 채택.
                row_best: tuple[float, float, float] | None = None
                for j, fb in enumerate(b_frames):
                    diff = _frame_mad(fa, fb)
                    if row_best is None or diff < row_best[2]:
                        row_best = (cut_a, b_offsets[j], diff)
                if row_best is None:
                    continue
                if threshold is not None and threshold > 0 and row_best[2] <= threshold:
                    return row_best
                if best is None or row_best[2] < best[2]:
                    best = row_best
            return best
        except Exception:
            # 유사도 탐색은 best-effort — 어떤 실패도 None(기존 트림 유지)으로 안전 처리.
            return None

    def _cut_clip_range(self, clip: str, start: float, end: float) -> str | None:
        """clip의 [start, end) 구간만 남긴 새 클립을 만들어 경로를 반환한다(실패 시 None).

        유사도 스티칭이 결정한 경계 컷 지점을 실제로 잘라내는 재인코딩. `_trim_tail_fixed`
        와 동일한 보편 호환 코덱/픽셀포맷 처방(yuv420p + High 프로파일)을 쓴다.
        """
        dur = end - start
        if dur < 0.3:
            return None
        import subprocess

        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        out = str(Path(self.settings.nutti_media_dir) / f"veo_fal_simcut_{uuid4().hex[:8]}.mp4")
        try:
            cut = subprocess.run(
                [ff, "-y", "-hide_banner", "-ss", f"{start:.3f}", "-i", clip,
                 "-t", f"{dur:.3f}", "-c:v", "libx264", "-profile:v", "high",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "aac", out],
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if cut.returncode != 0 or not Path(out).exists():
            return None
        return out

    def _extract_gray_frame_from_image(self, path: str) -> bytes | None:
        """이미지 파일 1장을 유사도 비교용 저해상도 그레이스케일 raw 바이트로 뽑는다.

        `_extract_gray_frames`의 이미지 단발 버전 — 꼬리 수렴 판정의 기준 프레임
        (고정 마스코트 프레임)을 뽑는 데 쓴다. 어떤 실패(ffmpeg 오류·짧은 출력)든 None을
        돌려 QC가 판단을 보류하도록 한다(best-effort, 파이프라인 비차단).
        """
        import subprocess

        import imageio_ffmpeg

        try:
            ff = imageio_ffmpeg.get_ffmpeg_exe()
            res = subprocess.run(
                [ff, "-hide_banner", "-i", path, "-vf",
                 f"scale={_SIM_W}:{_SIM_H},format=gray",
                 "-f", "rawvideo", "-frames:v", "1", "-"],
                capture_output=True,
                timeout=15,
            )
            raw = res.stdout or b""
            frame_size = _SIM_W * _SIM_H
            if len(raw) < frame_size:
                return None
            return raw[:frame_size]
        except Exception:
            return None

    def _qc_freeze_black(self, clip_path: str, dur: float) -> list[str]:
        """클립에서 중간 프리즈/블랙프레임 구간을 검출해 사유 리스트를 반환한다.

        ffmpeg freezedetect·blackdetect를 한 번에 돌려 stderr의 freeze/black 구간을
        파싱한다. 끝프레임 고정 모드는 클립이 같은 정적 마스코트 프레임에서 시작·종료하므로
        가장자리(`qc_edge_ignore_sec` 이내) 프리즈/블랙은 의도된 것 — 그 구간에 걸친 창은
        무시하고, 클립 중간에서 시작·종료하는 창만 결함으로 센다. 파싱/서브프로세스 실패는
        빈 리스트로 폴백한다(best-effort, 절대 파이프라인을 막지 않음).
        """
        import re
        import subprocess

        import imageio_ffmpeg

        try:
            ff = imageio_ffmpeg.get_ffmpeg_exe()
            edge = self.settings.qc_edge_ignore_sec
            vf = (
                f"freezedetect=n=-60dB:d={self.settings.qc_freeze_min_sec},"
                f"blackdetect=d={self.settings.qc_black_min_sec}:pic_th=0.98"
            )
            res = subprocess.run(
                [ff, "-hide_banner", "-i", clip_path, "-vf", vf, "-f", "null", "-"],
                capture_output=True,
            )
            err = (res.stderr or b"").decode("utf-8", "replace")

            def has_mid(starts: list[str], ends: list[str]) -> bool:
                for k, s in enumerate(starts):
                    st = float(s)
                    if k < len(ends):
                        # 양끝 다 있는 창: 클립 중간에서 시작·종료해야 결함(가장자리 정적
                        # 프레임은 정상).
                        if st > edge and float(ends[k]) < dur - edge:
                            return True
                    # 종료 라인 없음 = 회복 없이 EOF까지 지속. 끝 가장자리 전에 시작했으면
                    # 무조건 결함 — 중간~끝 내내 얼어붙은 최악 케이스(QC가 잡아야 할 바로
                    # 그 상황)를 en=dur로 면제하던 버그를 막는다(리뷰 지적, HIGH).
                    elif st < dur - edge:
                        return True
                return False

            reasons: list[str] = []
            if has_mid(
                re.findall(r"freeze_start:\s*([0-9.]+)", err),
                re.findall(r"freeze_end:\s*([0-9.]+)", err),
            ):
                reasons.append("mid_freeze")
            if has_mid(
                re.findall(r"black_start:\s*([0-9.]+)", err),
                re.findall(r"black_end:\s*([0-9.]+)", err),
            ):
                reasons.append("black_frame")
            return reasons
        except Exception:
            return []

    def _qc_tail_convergence(self, clip_path: str, frame_path: str, dur: float) -> str | None:
        """클립 꼬리가 고정 마스코트 프레임으로 수렴하지 못했으면 사유를, 아니면 None을 반환.

        끝프레임 고정 모드에서만 의미가 있다(모든 클립이 같은 프레임으로 수렴해야 함).
        마지막 `qc_tail_window_sec`초를 샘플링해, 마지막 프레임이 기준(고정) 프레임에서
        여전히 멀고(`mad_ref`) **동시에** 아직 눈에 띄게 변하는 중(`mad_delta`)일 때만
        "tail_not_converged"를 낸다 — 기준에 가깝거나, 다른 포즈지만 안정된 클립은 오검출
        하지 않는다(두 조건 AND). 샘플 부족·기준 추출 실패·바이트 길이 불일치·기타 실패는
        None(판단 보류)으로 돌려 파이프라인을 막지 않는다.
        """
        try:
            start = max(0.0, dur - self.settings.qc_tail_window_sec)
            tail = self._extract_gray_frames(clip_path, start, dur - start)
            ref = self._extract_gray_frame_from_image(frame_path)
            if len(tail) < 2 or ref is None:
                return None
            last = tail[-1]
            second = tail[-2]
            if len(last) != len(ref) or len(second) != len(last):
                return None
            mad_ref = _frame_mad(last, ref)
            mad_delta = _frame_mad(second, last)
            if (
                mad_ref > self.settings.qc_tail_converge_mad_max
                and mad_delta > self.settings.qc_tail_delta_max
            ):
                return "tail_not_converged"
            return None
        except Exception:
            return None

    def _qc_check_beat(
        self, clip_path: str, frame_path: str, lock: bool, *, final_beat: bool = False
    ) -> list[str]:
        """한 비트 클립의 QC 사유 리스트를 모아 반환한다(빈 리스트 = 통과).

        검사: ①중간 프리즈/블랙(_qc_freeze_black) ②무발화(트림 실측 발화 길이가
        `qc_min_speech_sec` 미만) ③(lock 모드, 마지막 비트 제외) 꼬리 미수렴
        (_qc_tail_convergence — `final_beat=True`면 면제: 뒤에 이어붙일 클립이 없고
        모션도 자유(_MOTION_FINAL_FREE)라 수렴 실패가 결함이 아님. 6차 런 실측:
        마지막 비트가 이 검사로 2회 재생성돼 $0.8 낭비)
        ④화면 텍스트(외계어 자막, _qc_text_overlay — 비용상 다른 사유 없을 때만).
        `_trim_to_speech`는 발화 길이 측정용으로만 호출하고, 새로 만든 트림 파일은 즉시
        삭제한다 — 실제 대량 트림은 기존 후처리에서 그대로 수행한다. 어떤 검사도 파이프라인을
        막지 않도록 best-effort로 동작한다(qc_enabled=False면 즉시 빈 리스트).
        """
        if not self.settings.qc_enabled:
            return []
        reasons: list[str] = []
        try:
            dur = self._probe_duration_sec(clip_path)
        except Exception:
            dur = None
        if dur is not None:
            reasons.extend(self._qc_freeze_black(clip_path, dur))
        trimmed, speech_sec = self._trim_to_speech(clip_path)
        if trimmed != clip_path:
            Path(trimmed).unlink(missing_ok=True)
        if speech_sec is not None and speech_sec < self.settings.qc_min_speech_sec:
            reasons.append("short_speech")
        if lock and not final_beat and dur is not None:
            tail_reason = self._qc_tail_convergence(clip_path, frame_path, dur)
            if tail_reason is not None:
                reasons.append(tail_reason)
        # ④ 화면 텍스트(외계어 자막) — Claude 비전 판정이라 가장 비싸므로, 다른 사유로
        # 이미 재생성이 확정된 클립은 건너뛴다(재생성본이 다음 QC 라운드에서 다시 검사됨).
        if not reasons and dur is not None:
            text_reason = self._qc_text_overlay(clip_path, dur)
            if text_reason is not None:
                reasons.append(text_reason)
        return reasons

    # 텍스트 QC용 프레임 샘플 수. Veo 가짜 자막은 자막 특성상 수 초간 지속되므로
    # 7초 클립 기준 ~1.2초 간격 샘플이면 놓치지 않는다(순간 플래시성은 범위 밖).
    _QC_TEXT_FRAMES = 6

    def _qc_text_overlay(self, clip_path: str, dur: float) -> str | None:
        """클립 프레임에 렌더된 글자(Veo 외계어 자막)가 보이면 "text_overlay"를 반환한다.

        프롬프트 3겹 방어(본문 금지문·_NEGATIVE·negative_prompt)로도 확률적으로 뚫리는
        Veo의 임의 화면 자막(실측: "칙하 아대되?" 등 깨진 한글)을 하드룰 원칙대로
        생성물 검사로 잡는다 — 검출 시 호출부의 기존 QC 재생성 루프가 그 비트만 다시
        만든다. 컬러 프레임 샘플 → Claude 비전 판정(주입 가능). 샘플 추출 실패·판정
        실패/보류는 None(통과) — 어떤 실패도 파이프라인을 막지 않는다(best-effort).
        """
        if not self.settings.qc_text_enabled:
            return None
        frames = self._extract_color_frames(clip_path, dur)
        if not frames:
            return None
        try:
            verdict = self._judge_frames_text(frames)
        finally:
            for f in frames:
                try:
                    Path(f).unlink(missing_ok=True)
                except OSError:
                    pass
        if verdict is True:
            log.warning("video.qc.text_overlay", clip=str(clip_path))
            return "text_overlay"
        return None

    def _extract_color_frames(self, clip_path: str, dur: float) -> list[str]:
        """클립에서 텍스트 판정용 컬러 프레임 PNG를 균등 간격으로 뽑는다(best-effort).

        판정 페이로드를 줄이려고 폭 360px로 축소한다 — 자막류 텍스트는 이 크기에서도
        충분히 판독된다(진단 실측). 실패 시 빈 리스트(호출부가 검사 보류).
        """
        if dur <= 0:
            return []
        import subprocess

        try:
            import imageio_ffmpeg

            out_dir = Path(self.settings.nutti_media_dir)
            pattern = out_dir / f"qc_text_{uuid4().hex[:8]}_%02d.png"
            n = self._QC_TEXT_FRAMES
            res = subprocess.run(
                [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", clip_path,
                 "-vf", f"fps={n}/{dur:.3f},scale=360:-2",
                 "-frames:v", str(n), str(pattern)],
                capture_output=True,
                timeout=60,
            )
            frames = sorted(str(p) for p in out_dir.glob(pattern.name.replace("%02d", "*")))
            if res.returncode != 0:
                for f in frames:
                    Path(f).unlink(missing_ok=True)
                return []
            return frames
        except Exception:
            return []

    def _judge_frames_text(self, frame_paths: list[str]) -> bool | None:
        """프레임들에 렌더된 글자가 있는지 판정한다(주입 우선, 기본 Claude 비전).

        dry_run이면 항상 보류(None) — 외부 호출 없는 결정적 시뮬레이션 계약 유지.
        판정자 오류도 None(보류)으로 삼킨다 — QC가 클립 생산을 죽이면 안 된다.
        """
        if self._text_judge is not None:
            return self._text_judge(frame_paths)
        if self.settings.dry_run:
            return None
        try:
            from nutti.integrations.ai_text import AITextClient

            return AITextClient(self.settings).judge_frames_have_text(frame_paths)
        except Exception:
            log.warning("video.qc.text_judge_failed")
            return None

    def _generate_and_trim_clip(
        self, client, prompt: str, current_frame: str, frame_path: str,
        lock: bool, seed: int | None,
    ) -> str:
        """비트 클립 1개를 생성하고 끝 잉여 고정 트림까지 마친 경로를 반환한다.

        QC 재생성이 같은 단계를 다시 밟을 수 있도록 "생성 + tail-trim"을 헬퍼로 묶었다.
        끝 잉여 구간(글리치·이상동작 온상)은 트림 후 원본을 즉시 삭제한다(잔존 방지).
        """
        if lock:
            # 시작·끝 모두 마스코트 프레임으로 고정(끝프레임 고정 모드).
            clip_path = client.generate(
                frame_path, prompt, last_frame_path=frame_path, seed=seed
            )
        else:
            clip_path = client.generate(current_frame, prompt, seed=seed)
        tail = self.settings.veo_fal_clip_tail_trim_sec
        if tail > 0:
            cut = self._trim_tail_fixed(clip_path, tail)
            if cut != clip_path:
                Path(clip_path).unlink(missing_ok=True)
                clip_path = cut
        return clip_path

    def _stitch(
        self,
        clips: list[str],
        durations: list[float | None] | None = None,
        *,
        boundary_dissolves: list[float] | None = None,
    ) -> str:
        """여러 8초 클립을 ffmpeg로 이어붙여 하나의 MP4로 만든다.

        `settings.veo_fal_crossfade_sec`>0 이고 모든 클립 길이를 알면 비트 경계에 짧은
        디졸브(xfade/acrossfade)를 줘 의상·구도 점프를 부드럽게 가린다(2026-06-29 PO
        옵션 B — 근본 제거가 아닌 완화). 길이를 모르거나 디졸브 ffmpeg이 실패하면 단순
        concat으로 안전 폴백한다. 클립 1개면 스티칭 없이 그대로 반환한다. ffmpeg 바이너리는
        imageio-ffmpeg 번들을 쓴다(시스템 설치 불요). 실패(ffmpeg 비정상 종료·미설치)는
        VideoRenderError 계약으로 변환하며, 입력 경로가 박힐 수 있는 stderr 원문은 노출하지
        않고 예외 타입명만 남긴다(redaction).
        """
        # 실제 적용된 디졸브 길이를 기록한다 — 자막 타이밍(_burn_captions)이 concat
        # 폴백(디졸브 0)과 디졸브 경로를 구분해야 경계 오차가 누적되지 않는다(리뷰 지적:
        # 폴백인데 디졸브 가정 시 경계 k에서 k×디졸브만큼 자막이 앞서간다).
        # 경계별 값(_last_boundary_dissolves)도 함께 기록한다 — 유사도 매칭 경계는
        # 마이크로 컷(0.08s), 미스매치 경계는 2배라 경계마다 달라(2026-07-10) 자막
        # 전환 시점이 대표값 하나로는 어긋난다.
        self._last_stitch_dissolve = 0.0
        self._last_boundary_dissolves: list[float] | None = None
        if len(clips) == 1:
            return clips[0]
        dissolve = float(getattr(self.settings, "veo_fal_crossfade_sec", 0.0) or 0.0)
        if dissolve > 0 and durations is not None and len(durations) == len(clips):
            faded = self._stitch_dissolve(
                clips, durations, dissolve, boundary_dissolves=boundary_dissolves
            )
            if faded is not None:
                self._last_stitch_dissolve = dissolve
                n = len(clips)
                self._last_boundary_dissolves = (
                    list(boundary_dissolves)
                    if boundary_dissolves is not None and len(boundary_dissolves) == n - 1
                    else [dissolve] * (n - 1)
                )
                return faded
        return self._concat(clips)

    def _input_norm(self, i: int) -> str:
        """스티칭 입력 i의 정규화 필터 체인(픽셀포맷·fps·SAR·해상도 + 교차 펀치인).

        모든 입력을 _STITCH_W×_STITCH_H로 통일해 xfade/concat 크기 불일치를 막는다.
        punch_in_scale>1이면 짝수 비트(0·2… — 훅 포함)를 확대 후 원 해상도로 크롭해
        컷마다 화면 크기가 교차되게 한다 — 동일 구도 점프컷을 의도된 편집으로 위장하고
        시각 리듬을 만든다(2026-07-06 PO). 크롭 세로 기준은 상단 1/3(얼굴 보존).
        """
        # setsar=1은 체인 마지막에 — 펀치인 scale의 짝수 반올림이 미세 비율 오차(<0.1%,
        # 비가시)를 만들어 SAR이 1:1이 아니게 기록되는 것을 방지한다(실측 2026-07-06).
        base = f"[{i}:v]format=yuv420p,fps=30"
        s = float(getattr(self.settings, "veo_fal_punch_in_scale", 0.0) or 0.0)
        if s > 1.0 and i % 2 == 0:
            w2 = int(_STITCH_W * s) // 2 * 2
            h2 = int(_STITCH_H * s) // 2 * 2
            return (
                f"{base},scale={w2}:{h2},"
                f"crop={_STITCH_W}:{_STITCH_H}:(iw-{_STITCH_W})/2:(ih-{_STITCH_H})/3,setsar=1"
            )
        return f"{base},scale={_STITCH_W}:{_STITCH_H},setsar=1"

    def _stitch_dissolve(
        self,
        clips: list[str],
        durations: list[float | None],
        dissolve: float,
        *,
        boundary_dissolves: list[float] | None = None,
    ) -> str | None:
        """클립 경계에 짧은 디졸브(xfade+acrossfade)를 줘 이어붙인다(best-effort).

        모든 클립 길이가 유효하고 디졸브보다 충분히 길 때만 offset 누적이 성립한다 —
        하나라도 길이를 모르거나 너무 짧으면 None을 돌려 호출부가 concat으로 폴백한다.
        디졸브 ffmpeg 실패(필터 비호환·타임아웃 등)도 None으로 안전 폴백. xfade는 입력
        해상도/fps/SAR가 같아야 하므로 각 비디오를 fps/format/SAR로 정규화한 뒤 체이닝한다.

        `boundary_dissolves`(경계별 길이, len=len(clips)-1)를 주면 경계마다 다른 디졸브를
        쓴다 — 유사도 매칭 경계는 마이크로 컷(_SIM_CUT_DISSOLVE), 불일치 경계는 2배
        (2026-07-07/07-10 PO). None(기본)이면 전 경계가 `dissolve`를 쓴다(기존 동작
        그대로). 자막 타이밍은 _stitch가 기록하는 경계별 실적용 값
        (`_last_boundary_dissolves`)으로 동기화된다.
        """
        n = len(clips)
        per_boundary = list(boundary_dissolves) if boundary_dissolves is not None else None
        if per_boundary is None or len(per_boundary) != n - 1:
            per_boundary = [dissolve] * (n - 1)
        dur: list[float] = []
        for idx, d in enumerate(durations):
            # 이 클립과 맞닿은 경계들(왼쪽·오른쪽) 중 더 큰 디졸브 길이 기준으로 충분한
            # 길이인지 검사한다 — 한쪽 경계가 2배로 늘어나도 offset이 음수가 되면 안 된다.
            neighbors = per_boundary[max(0, idx - 1):idx + 1]
            needed = max(neighbors) if neighbors else dissolve
            if d is None or d <= needed + 0.1:
                return None
            dur.append(float(d))
        import subprocess

        import imageio_ffmpeg

        out_path = Path(self.settings.nutti_media_dir) / f"video_{uuid4().hex[:12]}.mp4"
        inputs: list[str] = []
        for clip in clips:
            inputs += ["-i", clip]
        parts: list[str] = [f"{self._input_norm(i)}[v{i}]" for i in range(n)]
        # 비디오 xfade 체인: 클립 k 합류 시 offset = 직전 출력길이 - 그 경계의 디졸브.
        vlabel = "v0"
        cum = dur[0]
        for k in range(1, n):
            d_k = per_boundary[k - 1]
            offset = cum - d_k
            out = f"vx{k}"
            parts.append(
                f"[{vlabel}][v{k}]xfade=transition=fade:"
                f"duration={d_k:.3f}:offset={offset:.3f}[{out}]"
            )
            vlabel = out
            cum = cum + dur[k] - d_k
        # 오디오 acrossfade 체인: 경계에서 자동으로 끝-시작을 겹쳐 페이드(offset 불요).
        alabel = "0:a"
        for k in range(1, n):
            d_k = per_boundary[k - 1]
            out = f"ax{k}"
            parts.append(f"[{alabel}][{k}:a]acrossfade=d={d_k:.3f}[{out}]")
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
        # (yuv444p/yuv420p 혼재) 실패하므로 입력마다 yuv420p·30fps·SAR=1로 정규화한다
        # (교차 펀치인 포함 — 디졸브 폴백 경로에서도 화면 크기 교차가 유지되게).
        parts: list[str] = [f"{self._input_norm(i)}[cv{i}]" for i in range(n)]
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

    @staticmethod
    def _wrap_caption(text: str, width: int = 16) -> str:
        """대사를 자막용으로 공백 기준 줄바꿈한다(drawtext는 자동 줄바꿈이 없다).

        한 줄이 width자를 넘지 않게 단어 단위로 끊는다(단어 자체가 width보다 길면
        그 단어는 한 줄로 그대로 둔다 — 한국어 대사에서 사실상 발생하지 않음).
        """
        lines: list[str] = []
        cur = ""
        for word in text.split():
            cand = f"{cur} {word}".strip()
            if cur and len(cand) > width:
                lines.append(cur)
                cur = word
            else:
                cur = cand
        if cur:
            lines.append(cur)
        return "\n".join(lines)

    @staticmethod
    def _split_caption_segments(text: str) -> list[str]:
        """자막을 문장 단위 세그먼트로 분리한다 — 세그먼트별로 한 줄씩 순차 표시된다.

        문장 종결부호(.!?。…) 뒤 공백 기준. 구두점이 없어 분리가 안 되면 전체 텍스트를
        단일 세그먼트로 반환해 종전 동작(비트 전체 동시 표시)과 동일하게 폴백한다.
        """
        segs = [s.strip() for s in _CAPTION_SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
        return segs or ([text.strip()] if text.strip() else [])

    def _drawtext_filter(
        self,
        line: str,
        *,
        font_ff: str,
        size: int,
        y: str,
        media_dir: Path,
        txt_files: list[Path],
        enable: str | None = None,
    ) -> str | None:
        """한 줄짜리 drawtext 필터 문자열을 만든다(대사 textfile 생성 포함).

        하단 자막과 상단 훅 오버레이가 공유한다. `enable`이 None이면 영상 전체에
        표시된다. 폰트/텍스트 파일 경로에 작은따옴표가 있으면 None(자막 포기 신호 —
        호출부가 무자막 원본으로 폴백).
        """
        tf = media_dir / f"caption_{uuid4().hex[:8]}.txt"
        # newline='\n' 필수 — Windows 텍스트 모드가 \n을 \r\n으로 바꾸면
        # drawtext가 CR을 빈 줄로 렌더해 줄 간격이 두 배로 벌어진다(실측).
        tf.write_text(line, encoding="utf-8", newline="\n")
        txt_files.append(tf)
        tf_ff = str(tf).replace("\\", "/").replace(":", r"\:")
        if "'" in tf_ff or "'" in font_ff:
            log.warning("video.caption.path_quote")
            return None
        suffix = f":enable='{enable}'" if enable else ""
        return (
            f"drawtext=fontfile='{font_ff}':textfile='{tf_ff}':"
            f"fontsize={size}:fontcolor=white:"
            f"borderw={max(2, round(size / 10))}:bordercolor=black:"
            f"x=(w-text_w)/2:y={y}{suffix}"
        )

    def _find_caption_font(self) -> str | None:
        """자막 폰트 경로를 찾는다: 설정값 우선, 없으면 OS 기본 후보 순회. 없으면 None."""
        cands = [self.settings.caption_font] if self.settings.caption_font else []
        for cand in cands + _CAPTION_FONT_CANDIDATES:
            if cand and Path(cand).is_file():
                return _asciify_font_path(cand)
        return None

    def _burn_captions(
        self,
        video: str,
        beats: list[str],
        durations: list[float | None],
        dissolve: float = 0.0,
        boundary_dissolves: list[float] | None = None,
    ) -> str | None:
        """비트별 대사를 하단 한글 자막으로 굽는다(best-effort — 실패 시 None, 원본 유지).

        `dissolve`는 _stitch가 **실제 적용한** 디졸브 길이(concat 폴백이면 0)를 받는다 —
        설정값을 다시 읽으면 폴백 시 경계 k마다 k×디졸브만큼 자막이 앞서가는 누적 오차가
        생긴다. `boundary_dissolves`(경계별 실적용 값, len=len(beats)-1)가 오면 그걸
        우선한다 — 유사도 매칭 경계(마이크로 컷)와 미스매치 경계(2배)가 섞이면 대표값
        하나로는 전환 시점이 어긋난다(2026-07-10). 자막 전환 시점은 각 경계 디졸브의 중앙.

        문장 단위 순차 표시(2026-07-10 PO — 한 줄씩 넘어가는 스타일): 각 비트를
        `_split_caption_segments`로 문장 단위 세그먼트로 나누고, 비트의 표시 구간
        [start,end)를 세그먼트 글자 수 비율로 나눠 세그먼트마다 그 시간에만 보이게
        한다(발화 속도에 대한 근사 — 실제 음성 타임스탬프는 없음, 글자수 비례가
        가장 단순하고 충분히 정확한 근사). 구두점이 없어 분리가 안 되면 세그먼트가
        1개로 종전처럼 비트 전체 구간에 표시된다(하위호환).

        drawtext 이스케이프 지뢰를 피하려고 대사는 textfile(UTF-8)로 전달한다. 폰트가
        없거나 경로에 작은따옴표가 있으면 자막 없이 통과한다.
        """
        if not beats:
            return None
        font = self._find_caption_font()
        if font is None:
            log.warning("video.caption.no_font")
            return None
        # ffmpeg 필터 파서는 2단계다: 바깥(그래프) 파서가 따옴표를 소비한 뒤 drawtext의
        # 옵션 파서가 ':'로 다시 쪼갠다 — 드라이브 콜론(C:)은 따옴표만으로 못 지키고
        # 반드시 \: 로 이스케이프해야 한다(실측 2026-07-06: 미이스케이프 시 파스 실패).
        font_ff = str(font).replace("\\", "/").replace(":", r"\:")
        dur = [
            (d if d is not None else _CLIP_SEC)
            for d in (durations if len(durations or []) == len(beats) else [None] * len(beats))
        ]
        bd = (
            list(boundary_dissolves)
            if boundary_dissolves is not None and len(boundary_dissolves) == len(beats) - 1
            else [dissolve] * (len(beats) - 1)
        )
        starts = [0.0]
        cum = dur[0]
        for k in range(1, len(beats)):
            d_k = bd[k - 1]
            starts.append(max(0.0, cum - d_k / 2))
            cum += dur[k] - d_k
        ends = starts[1:] + [cum + 1.0]  # 마지막 자막은 영상 끝까지(여유 1초)
        import subprocess

        import imageio_ffmpeg

        media_dir = Path(self.settings.nutti_media_dir)
        out_path = media_dir / f"video_{uuid4().hex[:12]}.mp4"
        txt_files: list[Path] = []
        size = int(self.settings.caption_font_size)
        # 줄바꿈 폭은 글자 크기에 반비례(한글 글리프 폭 ≈ fontsize) — 화면 폭의 ~82%를
        # 넘지 않게. 큰 글씨일수록 적은 글자에서 줄을 바꾼다.
        wrap_width = max(8, int(_STITCH_W * 0.82 / size))
        line_h = round(size * 1.35)  # 줄 높이(자간 포함)
        try:
            filters: list[str] = []
            for k, beat in enumerate(beats):
                beat_start, beat_end = starts[k], ends[k]
                beat_width = max(0.0, beat_end - beat_start)
                segments = self._split_caption_segments(beat)
                total_chars = sum(len(seg) for seg in segments) or 1
                seg_start = beat_start
                for si, seg in enumerate(segments):
                    is_last = si == len(segments) - 1
                    seg_end = (
                        beat_end
                        if is_last
                        else seg_start + beat_width * len(seg) / total_chars
                    )
                    # 표시 텍스트는 끝 온점(.)을 뗀다(2026-07-10 PO — 캡션에 마침표가
                    # 거슬린다는 지적). 물음표·느낌표는 의미를 담으므로 남긴다. 문장
                    # 분리(_split_caption_segments)는 이 처리 전 원문 `seg`로 이미 끝났으므로
                    # 세그먼트 경계 판정에는 영향 없다 — 표시 시점의 순수 시각적 처리.
                    display_text = seg[:-1] if seg.endswith(".") else seg
                    # drawtext는 여러 줄을 블록 좌측 정렬로만 그린다(줄별 중앙정렬 미지원,
                    # 실측 2026-07-06) — 줄마다 독립 drawtext를 써서 각 줄을 중앙정렬한다.
                    # 블록 하단을 caption_y_pos(기본 960px — Shorts UI 회피, 2026-07-14 PO)에
                    # 고정(위로
                    # 쌓기)해 줄 수가 늘어도 화면 밖으로 잘리지 않는다(실측: 40px 4줄이
                    # 하단 잘림 — 여전히 유효한 가드, 기준점만 h*0.86→명시 픽셀로 변경).
                    lines = self._wrap_caption(display_text, width=wrap_width).split("\n")
                    for j, line in enumerate(lines):
                        y = f"{self.settings.caption_y_pos}-{(len(lines) - j) * line_h}"
                        f = self._drawtext_filter(
                            line, font_ff=font_ff, size=size, y=y,
                            media_dir=media_dir, txt_files=txt_files,
                            enable=f"between(t,{seg_start:.3f},{seg_end:.3f})",
                        )
                        if f is None:
                            return None
                        filters.append(f)
                    seg_start = seg_end
            # 상단 훅 오버레이(2026-07-16 PO — KR 쇼츠 무음 시청 대응): 훅 비트(①)
            # 첫 문장을 영상 전체 동안 상단에 크게 표시한다(정보성 쇼츠의 제목 오버레이
            # 관행). enable 없이 굽어 스크롤 중간 합류 시청자도 주제를 즉시 잡는다.
            if self.settings.hook_overlay:
                # 빈 비트 방어(리뷰 지적): 훅 문장이 없으면 오버레이만 건너뛰고
                # 하단 자막은 그대로 굽는다.
                hook_segs = self._split_caption_segments(beats[0])
                hook = hook_segs[0] if hook_segs else ""
                hook = hook[:-1] if hook.endswith(".") else hook
                hsize = int(self.settings.hook_font_size)
                hwrap = max(6, int(_STITCH_W * 0.9 / hsize))
                hline_h = round(hsize * 1.35)
                for j, line in enumerate(
                    self._wrap_caption(hook, width=hwrap).split("\n") if hook else []
                ):
                    f = self._drawtext_filter(
                        line, font_ff=font_ff, size=hsize,
                        y=str(int(self.settings.hook_y_pos) + j * hline_h),
                        media_dir=media_dir, txt_files=txt_files,
                    )
                    if f is None:
                        return None
                    filters.append(f)
            if not filters:
                # 전 비트가 빈 대사라 그릴 자막이 없다 — 빈 -vf로 ffmpeg를 부르지 않고
                # 무자막 원본 유지로 조기 폴백한다.
                return None
            cmd = [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-y",
                "-i", video,
                "-vf", ",".join(filters),
                # 비디오만 재인코딩(자막 픽셀 합성), 오디오는 무손실 통과.
                "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-movflags", "+faststart",
                str(out_path),
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            log.info("video.captions.burned", path=str(out_path), beats=len(beats))
            return str(out_path)
        except Exception:
            # 자막은 품질 개선용 best-effort — 어떤 실패도 무자막 원본으로 폴백한다
            # (_trim_tail_fixed·_chain_frame과 동일 관례. 좁은 except면 예기치 못한
            # 예외가 클립 생산 전체를 죽인다 — 리뷰 지적).
            Path(out_path).unlink(missing_ok=True)
            log.warning("video.caption.failed")
            return None
        finally:
            for tf in txt_files:
                try:
                    tf.unlink(missing_ok=True)
                except OSError:
                    pass

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
            # fallback_prompt: 주제의 신체 어휘가 FLUX 안전 필터를 오탐시키면(2026-07-14
            # 실측 — "엉덩이·항문낭"/"허리 라인"에 has_nsfw_concepts=True + placeholder)
            # 같은 프롬프트 재시도는 결정적으로 전부 실패한다. 재시도부터는 주제 문장을
            # 뺀 프롬프트로 전환한다 — 주제는 배경 연출용 부가 문맥이라 빠져도 무해.
            path = client.generate_frame(
                self._frame_prompt(script, style),
                reference_image_path=self.settings.nutti_mascot_image or None,
                fallback_prompt=self._frame_prompt(script, style, include_topic=False),
            )
        finally:
            if owned is not None:
                _close_owned(owned)
        log.info("video.frame.done", script_id=script.id)
        return path

    @staticmethod
    def _frame_prompt(script: Script, style: EpisodeStyle, *, include_topic: bool = True) -> str:
        """시작 프레임 생성용 장면 프롬프트(마스코트·세로 9:16·금지 요소 명시).

        `style`은 호출부(produce)가 한 번 계산해 비트 클립과 공유하는 편별
        의상·장소 — 여기서 독립 계산하지 않는다(프레임-클립 장면 일치 계약).
        주제도 AI 생성 텍스트이므로 `_sanitize_prompt_text`로 정제해 삽입한다
        (작은따옴표 치환 + 길이 제한 — 간접 프롬프트 주입 심층 방어).

        include_topic=False면 주제(Scene context) 문장을 뺀다 — 건강 주제의 신체
        어휘가 FLUX 안전 필터를 오탐시킬 때의 재시도 폴백용(generate_frame 참조).
        """
        topic = _sanitize_prompt_text(script.topic, _MAX_TOPIC_CHARS)
        # 주제는 AI 생성물 — 금지 리터럴(브랜드명 등)이 섞여 오면 크래시 대신 결정적으로
        # 제거하고 진행한다(리뷰 medium: 대본 파서는 회복형인데 프레임 가드만 무복구
        # 크래시인 설계 비대칭 해소). 사람이 직접 고치는 PO 수정 구역(의상·장소)은
        # 반대로 시끄럽게 실패(_validate_visual_prompt)하는 것이 맞다 — 의도된 비대칭.
        for banned in _PROMPT_BANNED_LITERALS:
            topic = re.sub(re.escape(banned), "", topic, flags=re.IGNORECASE)
        topic = " ".join(topic.split())
        # (아래 조립 결과는 반환 직전에 _validate_visual_prompt로 하드가드 — 2026-07-07 PO)
        # ===================== PO 수정 구역 (첫 장면 비주얼) =====================
        # 영상 "첫 장면의 구도·표정·마이크 연출"을 바꾸려면 아래 영어 묘사를 고친다.
        # 배경·의상은 위 로테이션 리스트(PO 수정 구역 — 편별 연출 로테이션)에서 고친다.
        # 마스코트 외형 자체는 NUTTI_MASCOT_IMAGE(레퍼런스 이미지)가 결정한다 — 여긴 구도/연출.
        # ASCII 작은따옴표(') 금지(주입 방어 검증과 충돌). 한국어로 원하는 그림만 정해도 됨.
        # 리터럴 "9:16"·브랜드명은 화면 자막으로 렌더되므로 넣지 않는다(세로 비율은 Kontext
        # aspect_ratio 파라미터가 담당). 캐릭터는 "진짜 실사 강아지"로 못박아 인형탈 방지.
        scene_context = f"Scene context: {topic}. " if include_topic else ""
        # 소품·포맷(2026-07-16 PO): 프레임은 FLF 끝프레임 고정의 앵커라 비트 클립과
        # 소품·마이크 유무가 일치해야 경계가 안 튄다 — build_beat의 scene 문장과 동일하게
        # 조립한다. interview 편은 마이크가 프레임에도 있어야 클립 시작·끝에서 마이크가
        # 나타났다 사라지는 점프가 없다.
        prop = f", with {style.prop}" if style.prop else ""
        # 구도·표정은 style.shot(선택 로직은 pick_episode_style로 일원화 — 연속 편 회피
        # 반영, 2026-07-23). 빈 문자열이면(레거시 EpisodeStyle 생성 코드·테스트) 종전
        # script.id 해시 선택으로 폴백해 결정성을 유지한다.
        shot = style.shot or (
            _FRAME_SHOTS[zlib.crc32(f"shot:{script.id}".encode()) % len(_FRAME_SHOTS)]
        )
        # 먹방 간식 그릇(2026-07-23 PO): 프레임은 FLF 앵커라 비트 클립과 그릇 유무가
        # 일치해야 경계에서 그릇이 나타났다 사라지는 점프가 없다(마이크와 동일 원리).
        food = (
            f"A small snack bowl with {script.food_visual} sits right in front of the "
            "puppy, clearly visible. "
            if script.food_visual
            else ""
        )
        if style.fmt == "interview":
            mic = (
                "A handheld interview microphone reaches into the frame from off-screen, "
                "pointed at the puppy; the person holding it stays completely out of frame. "
            )
        else:
            mic = "No microphone and no interview setup in frame. "
        prompt = (
            "A photorealistic tall vertical portrait-orientation starting frame for a "
            f"short-form video: {_MASCOT_APPEARANCE}, wearing {style.outfit}{prop}, "
            f"{style.setting}, "
            f"{shot}. {food}{_CINEMATIC_LOOK} "
            f"{scene_context}"
            # 첫 1초 무음 가독성(2026-07-21 쇼츠 트렌드): 0초 프레임만 보고도 상황이
            # 읽혀야 스와이프를 이긴다 — 배경·소품이 또렷이 보이는 상황 전달형 구도.
            "The setting and props are clearly visible so the situation reads at a glance. "
            "Absolutely no text, letters, numbers, words, captions, logos, brand names, or "
            "watermarks anywhere. No people, no humans in costume, no other animals. "
            f"{mic}"
        )
        # 하드가드: 금지 리터럴·작은따옴표 0개(대사 없음) — 과금 전 검증(2026-07-07 PO).
        _validate_visual_prompt(prompt, expected_quotes=0)
        return prompt
        # =================== PO 수정 구역 끝 (첫 장면 비주얼) ===================
