"""환경설정. `.env` 파일 또는 환경변수에서 로드한다.

`NUTTI_DRY_RUN=true`(기본값)이면 외부 API 키 없이도 전 파이프라인이
시뮬레이션으로 동작하므로, 키를 채우기 전에 구조부터 검증할 수 있다.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # 영상 백엔드: veo_fal(단일) — fal.ai 경유 Veo 3.1, 네이티브 한국어 음성 + 종량제.
    # 과거의 veo(Gemini API)·kling 백엔드는 2026-06-16 리팩토링에서 제거됐다.
    video_backend: Literal["veo_fal"] = Field(
        default="veo_fal", alias="NUTTI_VIDEO_BACKEND"
    )
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

    # ---- fal.ai Veo 3.1 백엔드(video_backend="veo_fal") ----
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
    veo_fal_crossfade_sec: float = Field(default=0.25, alias="NUTTI_VEO_FAL_CROSSFADE_SEC")
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

    # 저장소
    google_sheets_id: str = Field(default="", alias="GOOGLE_SHEETS_ID")
    google_service_account_json: str = Field(default="", alias="GOOGLE_SERVICE_ACCOUNT_JSON")

    # 검수
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    discord_webhook_url: str = Field(default="", alias="DISCORD_WEBHOOK_URL")
    # 검수 대기 동작
    review_timeout_sec: int = Field(default=3600, alias="NUTTI_REVIEW_TIMEOUT_SEC")
    review_poll_interval_sec: float = Field(default=3.0, alias="NUTTI_REVIEW_POLL_INTERVAL_SEC")
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

    # 간식계산기 고정 링크
    calculator_url: str = Field(
        default="https://wjdtjddns98.github.io/Nutti-Calculator/",
        alias="NUTTI_CALCULATOR_URL",
    )


@lru_cache
def get_settings() -> Settings:
    """프로세스 전역에서 재사용하는 설정 싱글턴."""
    return Settings()
