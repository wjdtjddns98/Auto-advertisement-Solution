"""환경설정. `.env` 파일 또는 환경변수에서 로드한다.

`NUTTI_DRY_RUN=true`(기본값)이면 외부 API 키 없이도 전 파이프라인이
시뮬레이션으로 동작하므로, 키를 채우기 전에 구조부터 검증할 수 있다.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 공통
    env: str = Field(default="dev", alias="NUTTI_ENV")
    log_level: str = Field(default="INFO", alias="NUTTI_LOG_LEVEL")
    dry_run: bool = Field(default=True, alias="NUTTI_DRY_RUN")

    # 1단계: 대본 (Claude 단일화 — 2026-06-16 PO 롤백 후 Gemini 텍스트 경로 제거).
    # 라이브(비-dry_run) 텍스트 생성: ANTHROPIC_API_KEY 있으면 Anthropic API(빠르고 안정적,
    # 권장), 없으면 claude -p CLI 폴백(Max 구독·무료지만 호출마다 세션 부팅으로 느리고 간헐
    # 타임아웃 가능). 대본·주제·팩트체크·메타데이터·성과분석 전부 Claude 경로를 쓴다.
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    script_model: str = Field(default="claude-opus-4-8", alias="NUTTI_SCRIPT_MODEL")
    # 실행 간 영속 상태(직전 성과 피드백·최근 주제) 저장 경로.
    # 매 사이클의 성과 분석을 다음 사이클 feedback으로 자동 연결하고,
    # 최근 주제를 기억해 주제 자동 생성 시 중복을 피하는 데 쓴다.
    state_path: str = Field(default="data/pipeline_state.json", alias="NUTTI_STATE_PATH")
    # 성과 수집 지연(시간). 업로드 직후 YouTube Analytics는 최근 1~3일치를 아직 집계하지
    # 않아 조회수·시청시간이 0으로 나온다(실측 2026-07-08: 전날 업로드분 API 조회 전부 0).
    # 그 0을 다음 대본 피드백으로 저장하면 루프가 노이즈로 오염된다 — 업로드는 대기 큐에
    # 쌓고, 이 시간(기본 48h)이 지난 건만 조회해 분석한다. dry_run은 지연 없이 즉시 수집.
    analytics_min_age_hours: int = Field(default=48, ge=0, alias="NUTTI_ANALYTICS_MIN_AGE_HOURS")
    # 사이클별 제작 비용을 누적 기록하는 원장(ledger) 경로. `nutti cost`로 일/월/전체
    # 실제 지출을 조회한다(dry_run 실행은 실제 지출 0으로 기록·구분).
    cost_ledger_path: str = Field(
        default="data/cost_ledger.json", alias="NUTTI_COST_LEDGER_PATH"
    )

    # 2단계: 영상 (FLUX.1 Kontext 시작 프레임 → fal.ai Veo 3.1 image-to-video)
    # 마스코트 레퍼런스 이미지 경로(없으면 텍스트 프롬프트만으로 시작 프레임 생성).
    nutti_mascot_image: str = Field(default="", alias="NUTTI_MASCOT_IMAGE")
    # 생성된 프레임/영상을 저장하는 로컬 디렉터리(fal 산출물은 일정 시간 후 삭제되므로 즉시 저장).
    nutti_media_dir: str = Field(default="data/media", alias="NUTTI_MEDIA_DIR")

    # 영상 백엔드는 veo_fal 단일이다(과거 veo/kling 백엔드·NUTTI_VIDEO_BACKEND 선택지는
    # 2026-06/07 리팩토링에서 제거).
    # fal.ai 단일 키 — 프레임(Kontext)·영상(Veo) 모두 FAL_KEY 하나로 처리. fal.ai 대시보드 발급.
    fal_key: str = Field(default="", alias="FAL_KEY")

    # ---- fal.ai FLUX.1 Kontext 프레임 생성 ----
    # 영상 시작 프레임을 FLUX.1 Kontext [pro]로 생성한다. 마스코트 레퍼런스 이미지를
    # image_url로 넣고 의상·장소 프롬프트를 주면 마스코트를 유지한 채 편집한다.
    # FAL_KEY를 재사용한다 — 추가 키 불요.
    kontext_model: str = Field(
        default="fal-ai/flux-pro/kontext", alias="NUTTI_KONTEXT_MODEL"
    )
    kontext_poll_interval_sec: float = Field(
        default=3.0, alias="NUTTI_KONTEXT_POLL_INTERVAL_SEC"
    )
    kontext_timeout_sec: float = Field(
        default=120.0, alias="NUTTI_KONTEXT_TIMEOUT_SEC"
    )

    # ---- fal.ai Veo 3.1 백엔드(veo_fal) ----
    # Veo 3.1을 fal.ai 종량제로 호스팅해 네이티브 한국어 음성·마스코트 일관성을 유지한다.
    # Lite 화질로 싸게 검증하고, Fast로 승격할 때는 모델명만 바꾼다(PO 승인 후).
    veo_fal_model: str = Field(
        default="fal-ai/veo3.1/lite/image-to-video",
        alias="NUTTI_VEO_FAL_MODEL",
    )
    # fal 큐 폴링 간격(초). Veo 생성이 오래 걸릴 수 있으므로 넉넉히.
    veo_fal_poll_interval_sec: float = Field(default=10.0, alias="NUTTI_VEO_FAL_POLL_INTERVAL_SEC")
    # fal 큐 전체 타임아웃(초). Veo 3.1은 최대 ~10분 소요를 대비한다.
    veo_fal_timeout_sec: float = Field(default=600.0, alias="NUTTI_VEO_FAL_TIMEOUT_SEC")
    # fal Veo 출력 해상도. "720p"(기본·저비용) | "1080p"(고품질).
    veo_fal_resolution: str = Field(default="720p", alias="NUTTI_VEO_FAL_RESOLUTION")
    # fal Veo 제출 시 보낼 negative_prompt — 화면에 텍스트(특히 깨진 한글 자막)를 임의로
    # 렌더하는 것을 억제한다(대사는 음성으로만). Veo가 대사 프롬프트를 받을 때 자막을
    # 그리는 경향에 대한 핵심 방어(프롬프트 본문의 "no on-screen text" 지시와 이중 방어).
    # 빈 값으로 두면 negative_prompt를 제출 페이로드에서 생략한다.
    veo_fal_negative_prompt: str = Field(
        default=(
            "text, subtitles, captions, words, letters, writing, watermark, "
            "on-screen text, caption bar, hardcoded subtitles, korean text overlay, "
            "hangul, hangul characters, korean characters, korean subtitles, "
            "subtitle bar at bottom, bottom subtitle, lower third text, "
            # 클립 끝 페이드아웃 억제 — 끝 프레임이 어두워지면 프레임 체이닝이 망가진다.
            "fade out, fade to black, fade in, dimming, darkening, vignette, "
            # 클립 끝 자세 변화(누움·이탈) 억제 — 비트 경계 점프의 주원인.
            "lying down, walking out of frame, leaving the frame, camera movement, camera zoom, "
            # 배경음악(BGM) 억제 — Veo가 발화 후 남는 잉여 구간을 음악으로 채우면(2026-06-29
            # PO 실측) 무음 트림(_trim_to_speech)이 발화 끝을 못 잡아 끝부분 헛짓(자세 무너짐·
            # 화면전환)이 그대로 남는다. 음악을 빼 잉여를 무음으로 되돌려 트림이 잘라내게 한다.
            "background music, music, instrumental, soundtrack, song, melody, jingle, "
            "sound effects, musical score, humming, singing, "
            # 막바지 헛짓/글리치 억제(2026-06-29 PO) — 끝 잉여 구간에서 자세가 급변하거나
            # 프레임이 뭉개지는(모핑/워핑/글리치) 현상을 직접 억제. 프롬프트 본문의 "끝 2~3초
            # 완전 정지"와 이중 방어.
            "sudden movement, sudden pose change, jerky motion, twitching, spasm, "
            "morphing, warping, distortion, deformed body, flickering, glitch, jitter"
        ),
        alias="NUTTI_VEO_FAL_NEGATIVE_PROMPT",
    )
    # 비트 클립을 이어붙일 때 경계에 줄 크로스페이드(디졸브) 길이(초). veo가 클립마다
    # 확률적으로 의상·구도를 살짝 바꿔 비트 경계에서 점프가 보일 수 있는데, 짧은 디졸브로
    # 그 순간을 부드럽게 가린다(근본 제거가 아닌 완화 — 2026-06-29 PO 옵션 B). 0이면
    # 디졸브 없이 단순 concat. 너무 길면 대사가 겹쳐 잘리므로 0.2~0.4초 권장.
    veo_fal_crossfade_sec: float = Field(default=0.35, alias="NUTTI_VEO_FAL_CROSSFADE_SEC")
    # 시각 리듬용 펀치인(디지털 줌) 최대 배율. 1.0 이하면 비활성.
    # 연혁: 비트 단위 교차 줌(2026-07-06) → 비활성(2026-07-10 PO "비트마다 크기가
    # 들쭉날쭉") → 2026-07-29 시간 스텝 방식으로 부활. 종전 문제는 "8초에 한 번, 비트
    # 단위로" 크기가 바뀌어 연속성 파괴로만 읽힌 것 — 이번엔 클립 안에서
    # punch_in_period_sec마다 작은 폭으로 단계를 밟아 편집 리듬으로 읽히게 한다
    # (2026 쇼츠 잔존 데이터: 시각 변화 1.5~2초 주기가 하강 곡선을 플래토로 바꾼다).
    # 진폭이 크면 예전 지적이 재발하므로 1.10~1.15 범위를 지킬 것.
    # 2026-07-29 PO 실물 판정: 2초 주기 줌은 "화면전환이 너무 잦아 눈이 아프다" → 기본
    # 비활성(1.0)으로 되돌림. 잔존 벤치마크(1.5~2초 주기)보다 PO 육안 판정이 우선이다.
    # 다시 켤 때는 진폭·주기를 함께 낮춰서(예: SCALE=1.06, PERIOD=4) 시작할 것.
    # 2026-07-31 PO "지루하지 않게": 위 주석이 남긴 권고값 그대로 재활성(1.06/4초).
    # 종전 반려는 진폭 1.10~1.15 × 2초 주기의 과한 조합이었다 — 이번엔 진폭을 절반 가까이,
    # 주기를 2배로 늘려 "눈이 아픈" 축을 둘 다 낮췄다. 다시 반려되면 .env의
    # NUTTI_VEO_FAL_PUNCH_IN_SCALE=1.0 한 줄로 즉시 되돌릴 수 있다.
    veo_fal_punch_in_scale: float = Field(default=1.06, alias="NUTTI_VEO_FAL_PUNCH_IN_SCALE")
    # 펀치인 줌 단계가 바뀌는 주기(초). 0 이하면 시간 스텝 없이 클립 전체 고정 줌
    # (= 종전 비트 단위 동작). 1.2초 미만은 노이즈로 읽히므로 1.5~2.5초 권장.
    veo_fal_punch_in_period_sec: float = Field(
        default=4.0, alias="NUTTI_VEO_FAL_PUNCH_IN_PERIOD_SEC"
    )
    # B롤 인서트(2026-07-31 PO "장면전환·지루하지 않게"). 비트당 1회, 스티칭된 영상의
    # **비디오 트랙만** 짧게 이미지 컷으로 덮는다 — 오디오는 원본 그대로라 립싱크·자막
    # 타이밍이 영향받지 않는다. 컷 수가 비트 수(3)에서 3배로 늘어 시각 리듬이 생긴다.
    # 이미지는 마스코트 레퍼런스를 쓰는 기존 Kontext 경로를 재사용한다(화풍·색감 자동 정합).
    # 생성·합성 실패는 best-effort로 무시하고 원본을 그대로 쓴다(런이 죽지 않는다).
    # 끄려면 NUTTI_VEO_FAL_BROLL=false 한 줄.
    veo_fal_broll: bool = Field(default=True, alias="NUTTI_VEO_FAL_BROLL")
    # 비트별 대사를 하단 한글 자막으로 굽기(스티칭 후 ffmpeg drawtext, best-effort).
    # 기본 True — 2줄/26px 렌더 결과를 PO가 승인(2026-07-07, 최초 "이상함" 판정 시의
    # 렌더 결함은 26px 수정으로 이미 해소됨). Veo가 임의로 박는 깨진 자막은
    # negative_prompt로 계속 막는다(별개 방어).
    caption_burn: bool = Field(default=True, alias="NUTTI_CAPTION_BURN")
    # 자막 폰트 파일 경로. 비우면 OS 기본 후보 탐색(2026-07-10 PO 지시로 '여기어때
    # 잘난체'가 최우선 — assets/fonts/yg-jalnan.otf, 상업용 무료 폰트지만 "파일 배포"는
    # 라이선스 금지라 이 public 저장소엔 커밋하지 않고 로컬에만 둔다(.gitignore). 그
    # 파일이 없는 환경(CI·새 클론·Docker)은 맑은고딕 → Noto CJK → 나눔 순으로 폴백.
    caption_font: str = Field(default="", alias="NUTTI_CAPTION_FONT")
    # 자막 글자 크기(px, 720px 폭 기준). 26px가 "너무 작다"(2026-07-10 PO)는 지적으로
    # 34px로 상향(화면 폭의 ~4.7%). 이전 40px "너무 크다"(2026-06-06) 판정보다는 작게.
    # 테두리 두께는 크기에 비례해 자동 산출.
    # ge=1: 0이면 _burn_captions의 wrap_width 나눗셈이 ZeroDivisionError로 클립 생산을
    # 죽인다(리뷰 지적) — 설정 로드 시점에 시끄럽게 거부한다(과금 전 fail-fast).
    caption_font_size: int = Field(default=34, ge=1, alias="NUTTI_CAPTION_FONT_SIZE")
    # 자막 하단 기준 y좌표(px, 1280px 높이 기준). 1200px(2026-07-10)은 YouTube Shorts
    # UI(제목·채널명 오버레이, 하단 ~240px)에 깔리는 실측 문제로 1040px로 올렸으나
    # (2026-07-13), 프로덕션 편(0SVBKNkGoA4) 육안 확인 결과 여전히 낮다는 PO 판정
    # (2026-07-14) — 하단 25% 지점인 960px로 추가 상향.
    caption_y_pos: int = Field(default=960, ge=1, alias="NUTTI_CAPTION_Y_POS")
    # 상단 훅 텍스트 오버레이(2026-07-16 PO — KR 쇼츠 트렌드: 시청의 85%가 무음 시작이라
    # 훅은 음성이 아닌 화면 텍스트로 꽂아야 함). 훅 비트(①) 첫 문장을 영상 전체 동안
    # 화면 상단에 크게 굽는다. 자막(_burn_captions)과 같은 best-effort 계약.
    hook_overlay: bool = Field(default=True, alias="NUTTI_HOOK_OVERLAY")
    # 훅 오버레이 글자 크기(px, 720px 폭 기준). 하단 자막(34px)보다 크게 — 제목 역할.
    hook_font_size: int = Field(default=48, ge=1, alias="NUTTI_HOOK_FONT_SIZE")
    # 훅 오버레이 첫 줄 상단 y(px, 1280px 높이 기준). Shorts 상단 UI(검색·카메라 아이콘,
    # ~150px)를 피해 그 아래에 둔다.
    hook_y_pos: int = Field(default=200, ge=1, alias="NUTTI_HOOK_Y_POS")
    # 비트 경계 끊김(클립이 8초 동안 포즈가 drift해 다음 클립과 안 이어짐)을 근본적으로
    # 줄이기 위한 "끝프레임 고정" 모드(2026-06-29 PO 아이디어). True면 image-to-video
    # 대신 first-last-frame-to-video 모델을 써 각 비트 클립의 시작·끝 프레임을 동일한
    # 마스코트 프레임으로 고정한다 — 모든 클립이 같은 포즈로 시작·종료해 경계가 항상
    # 같은 프레임에서 만난다(체이닝 불요). False면 기존 image-to-video 경로(프레임 체이닝).
    veo_fal_endframe_lock: bool = Field(default=True, alias="NUTTI_VEO_FAL_ENDFRAME_LOCK")
    # 끝프레임 고정 모드에서 쓰는 first-last-frame-to-video 모델 ID. image-to-video와
    # 단가 동일($0.05/초·720p). 입력 필드가 다르다(image_url 대신 first_frame_url +
    # last_frame_url). endframe_lock=True일 때만 사용.
    veo_fal_flf_model: str = Field(
        default="fal-ai/veo3.1/lite/first-last-frame-to-video",
        alias="NUTTI_VEO_FAL_FLF_MODEL",
    )
    # 영상 내 비트(n1~n4) 음색 일관성 보강용 seed(2026-06-29 PO). Veo는 voice/reference
    # 파라미터가 없어 음색이 비트마다 드리프트하는데, 같은 seed + 같은 음색 프롬프트를 모든
    # 비트에 주면 편차가 줄어든다(seed가 오디오를 완전 통제하진 않으나 부분 효과 — 무료 카드).
    # None(기본)이면 _produce_clips_veo_fal가 영상마다 seed 1개를 뽑아 그 영상의 모든 비트에
    # 재사용한다(영상 내 일관, 영상 간 다양성 유지). 정수면 항상 그 값(영상 간에도 고정).
    veo_fal_seed: int | None = Field(default=None, alias="NUTTI_VEO_FAL_SEED")
    # 각 비트 클립 끝에서 무조건 잘라낼 초(2026-06-29 PO). 끝 잉여 구간 글리치 제거용
    # 안전판이지만, 고정값이면 대본마다 발화 종료 시점이 달라 8초 꽉 찬 대본은 대사가
    # 잘린다(PO 지적). 그래서 기본 0(비활성) — 발화 종료를 자동 감지하는 적응 무음 트림
    # (_trim_to_speech)에 맡긴다. 적응 트림은 발화 끝 기준이라 대본 길이와 무관하게 대사를
    # 보존하며 발화 후 글리치 구간만 자른다. 특정 운영에서 강제 상한이 필요하면 >0으로 켠다.
    veo_fal_clip_tail_trim_sec: float = Field(
        default=0.0, alias="NUTTI_VEO_FAL_CLIP_TAIL_TRIM_SEC"
    )
    # 비트 경계 유사도 스티칭 판단 임계(2026-07-07 PO 지시). 고정 지점 트림 대신, 경계
    # 근처 프레임 쌍의 평균절대차(MAD, 픽셀당 0~255)를 계산해 이 값 이하면 그 프레임 쌍에서
    # 실제로 이어붙인다. 초과하면 기존 트림을 유지하되 해당 경계만 크로스페이드를 2배로
    # 늘려 완화한다(_find_similarity_cuts/_produce_clips_veo_fal). 0 이하면 유사도 스티칭을
    # 완전히 끄고 기존 고정 트림 경로만 쓴다.
    stitch_sim_threshold: float = Field(default=18.0, alias="NUTTI_STITCH_SIM_THRESHOLD")

    # 비트 클립 QC 레이어(2026-07-07 PO 지시). 각 비트 클립이 스티칭에 도달하기 전에
    # 중간 프리즈·블랙프레임·무발화·꼬리 미수렴을 잡아 그 비트만 재생성한다(상한 초과 시
    # 현행 트림·마스킹 폴백). 아래 MAD 임계값들은 미검증 기본값으로, 6차 라이브 런에서 실측
    # 보정할 예정이다 — 지금은 보수적으로 두어 정상 클립을 오검출하지 않는 쪽에 무게를 둔다.
    qc_enabled: bool = Field(default=True, alias="NUTTI_QC_ENABLED")
    qc_max_retries: int = Field(default=2, alias="NUTTI_QC_MAX_RETRIES")
    qc_freeze_min_sec: float = Field(default=0.5, alias="NUTTI_QC_FREEZE_MIN_SEC")
    qc_black_min_sec: float = Field(default=0.3, alias="NUTTI_QC_BLACK_MIN_SEC")
    qc_edge_ignore_sec: float = Field(default=0.5, alias="NUTTI_QC_EDGE_IGNORE_SEC")
    qc_min_speech_sec: float = Field(default=1.0, alias="NUTTI_QC_MIN_SPEECH_SEC")
    qc_tail_window_sec: float = Field(default=1.0, alias="NUTTI_QC_TAIL_WINDOW_SEC")
    qc_tail_converge_mad_max: float = Field(
        default=20.0, alias="NUTTI_QC_TAIL_CONVERGE_MAD_MAX"
    )
    qc_tail_delta_max: float = Field(default=8.0, alias="NUTTI_QC_TAIL_DELTA_MAX")
    # 화면 텍스트(외계어 자막) QC(2026-07-10 PO "절대 안 생기게"). Veo가 프레임 안에
    # 임의로 그리는 깨진 한글 자막은 프롬프트 3겹 방어(본문 금지문 + _NEGATIVE +
    # negative_prompt 전송)로도 확률적으로 뚫린다(실측: "칙하 아대되?" 등) — 하드룰
    # 원칙(AI 생성물=회복형 재생성)대로 비트 클립 프레임을 Claude 비전으로 판정해
    # 검출 시 그 비트만 재생성한다. qc_enabled와 AND. 판정 실패는 보류(파이프라인 무해).
    qc_text_enabled: bool = Field(default=True, alias="NUTTI_QC_TEXT_ENABLED")

    # 저장소
    google_sheets_id: str = Field(default="", alias="GOOGLE_SHEETS_ID")
    google_service_account_json: str = Field(default="", alias="GOOGLE_SERVICE_ACCOUNT_JSON")

    # 검수 (텔레그램 단일 채널 — Discord 게이트는 2026-07-17 미완성 스캐폴딩 정리로 제거)
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    # 검수 대기 동작
    review_timeout_sec: int = Field(default=3600, alias="NUTTI_REVIEW_TIMEOUT_SEC")
    review_poll_interval_sec: float = Field(default=3.0, alias="NUTTI_REVIEW_POLL_INTERVAL_SEC")
    # 반려 사유 한 줄 입력을 기다리는 시간(초, 2026-07-29 PO). 사유 입력은 선택이라
    # 검수 대기(review_timeout_sec)와 달리 짧게 둔다 — 안 적으면 그냥 넘어간다.
    reject_reason_timeout_sec: int = Field(default=180, alias="NUTTI_REJECT_REASON_TIMEOUT_SEC")
    review_store_path: str = Field(default="data/reviews.json", alias="NUTTI_REVIEW_STORE_PATH")

    # 4단계: 업로드
    youtube_client_id: str = Field(default="", alias="YOUTUBE_CLIENT_ID")
    youtube_client_secret: str = Field(default="", alias="YOUTUBE_CLIENT_SECRET")
    youtube_refresh_token: str = Field(default="", alias="YOUTUBE_REFRESH_TOKEN")
    # 업로드 공개 범위: "public"(운영 기본) | "unlisted" | "private".
    # 첫 라이브 검증은 "private"로 override해 채널에 공개 노출 없이 안전하게 확인한다.
    # Literal로 제한해 오타("privat" 등)를 Settings 생성 시점에 잡는다(쿼터 낭비 방지).
    youtube_privacy_status: Literal["public", "unlisted", "private"] = Field(
        default="public", alias="NUTTI_YOUTUBE_PRIVACY_STATUS"
    )
    # --- 알고리즘 노출 최적화 메타데이터 ---
    # YouTube 카테고리 ID. 애견 콘텐츠는 15(Pets & Animals)가 추천 노출에 가장 적합.
    # (참고: 22=People & Blogs, 24=Entertainment). 문자열 ID로 보낸다.
    youtube_category_id: str = Field(default="15", alias="NUTTI_YOUTUBE_CATEGORY_ID")
    # 영상 언어(제목/설명·음성). 한국 시청자 추천 타겟팅을 위해 ko 기본.
    # defaultLanguage·defaultAudioLanguage 양쪽에 쓴다.
    youtube_default_language: str = Field(
        default="ko", alias="NUTTI_YOUTUBE_DEFAULT_LANGUAGE"
    )
    # 아동용 콘텐츠 선언(COPPA). 애견 콘텐츠는 보통 False — True면 댓글·알림·맞춤광고가
    # 제한돼 알고리즘 노출에 불리하다. 업로드 시 status.selfDeclaredMadeForKids로 명시.
    youtube_made_for_kids: bool = Field(
        default=False, alias="NUTTI_YOUTUBE_MADE_FOR_KIDS"
    )
    instagram_access_token: str = Field(default="", alias="INSTAGRAM_ACCESS_TOKEN")
    instagram_account_id: str = Field(default="", alias="INSTAGRAM_ACCOUNT_ID")

    # 간식계산기 고정 링크(2026-07-21 nutti.co.kr DNS 전환 — PO 지시).
    # UTM 추적 파라미터는 여기 넣지 않는다 — _build_metadata가 편별(utm_content=script.id)로 붙인다.
    calculator_url: str = Field(
        default="https://nutti.co.kr/calculator.html",
        alias="NUTTI_CALCULATOR_URL",
    )

    # ---- 업로드 직후 자동 댓글(2026-07-30 PO "간식계산기 링크는 설명란에 넣고 댓글에 자동으로") ----
    # 대사(CTA)에서는 유도를 전면 금지했으므로(validate_script_body의 _BANNED_CTA_WORDS)
    # 계산기 유입 경로는 설명란과 이 댓글 둘뿐이다.
    # ⚠️ 댓글 **고정(pin)** 은 YouTube Data API가 제공하지 않는다 — 자동으로 달리기만 하고,
    # 상단 고정이 필요하면 Studio에서 수동으로 해야 한다.
    # ⚠️ 이 기능은 `youtube.force-ssl` 스코프가 필요하다(업로드 전용 스코프로는 403).
    # 스코프가 없으면 댓글만 실패하고 업로드는 성공으로 유지된다(best-effort).
    youtube_auto_comment: bool = Field(default=True, alias="NUTTI_YOUTUBE_AUTO_COMMENT")
    # 댓글 본문. 링크는 코드가 UTM(utm_medium=comment)을 붙여 뒤에 이어붙인다 —
    # 설명란(utm_medium=shorts)과 구분해야 어느 경로가 유입을 만드는지 GA에서 갈린다.
    youtube_comment_text: str = Field(
        default="우리 아이 몸무게로 하루 간식량 계산해보기 🐾",
        alias="NUTTI_YOUTUBE_COMMENT_TEXT",
    )


@lru_cache
def get_settings() -> Settings:
    """프로세스 전역에서 재사용하는 설정 싱글턴."""
    return Settings()
