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

    # 1단계: 대본 (기본 Claude — 2026-06-16 PO 롤백. Gemini 텍스트가 대본 잘림·팩트체크
    # FAIL이 잦아 #57 이전 Claude 경로로 되돌렸다.)
    # text_backend: 라이브(비-dry_run) 텍스트 생성 백엔드 선택.
    #   "claude"(기본) → ANTHROPIC_API_KEY 있으면 Anthropic API(빠르고 안정적, 권장),
    #     없으면 claude -p CLI 폴백(Max 구독·무료지만 호출마다 세션 부팅으로 느리고 간헐
    #     타임아웃 가능). 대본·주제·팩트체크·메타데이터·성과분석 전부.
    #   "gemini" → GEMINI_API_KEY로 Gemini generateContent 호출(가볍고 추가 키 불필요하나
    #     대본 잘림·팩트체크 FAIL 이슈로 기본에서 내려옴). 키 없으면 claude 폴백으로 강등.
    text_backend: Literal["gemini", "claude"] = Field(
        default="claude", alias="NUTTI_TEXT_BACKEND"
    )
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    script_model: str = Field(default="claude-opus-4-8", alias="NUTTI_SCRIPT_MODEL")
    # text_backend="gemini"일 때 대본·팩트체크 등에 쓰는 Gemini 텍스트 모델(저비용 flash).
    gemini_text_model: str = Field(
        default="gemini-2.5-flash", alias="NUTTI_GEMINI_TEXT_MODEL"
    )
    # 실행 간 영속 상태(직전 성과 피드백·최근 주제) 저장 경로.
    # 매 사이클의 성과 분석을 다음 사이클 feedback으로 자동 연결하고,
    # 최근 주제를 기억해 주제 자동 생성 시 중복을 피하는 데 쓴다.
    state_path: str = Field(default="data/pipeline_state.json", alias="NUTTI_STATE_PATH")
    # 사이클별 제작 비용을 누적 기록하는 원장(ledger) 경로. `nutti cost`로 일/월/전체
    # 실제 지출을 조회한다(dry_run 실행은 실제 지출 0으로 기록·구분).
    cost_ledger_path: str = Field(
        default="data/cost_ledger.json", alias="NUTTI_COST_LEDGER_PATH"
    )

    # 2단계: 영상 (Gemini 이미지 → Veo 3.1 image-to-video)
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_image_model: str = Field(
        default="gemini-2.5-flash-preview-05-20", alias="NUTTI_GEMINI_IMAGE_MODEL"
    )
    # 기본 Fast($0.10/초·720p): 다중 비트 연속 영상이 Veo extend(영상 연장)를 쓰는데
    # extend는 Fast/Standard만 지원하고 Lite는 미지원이라 Fast를 기본으로 둔다
    # (2026-06 ai.google.dev/gemini-api/docs/video). Lite($0.05) 대비 ~2배 비용.
    # Fast는 negativePrompt 지원(VeoClient가 모델명으로 분기). 더 높은 화질은 standard로.
    veo_model: str = Field(default="veo-3.1-fast-generate-preview", alias="NUTTI_VEO_MODEL")
    # 마스코트 레퍼런스 이미지 경로(없으면 텍스트 프롬프트만으로 시작 프레임 생성).
    nutti_mascot_image: str = Field(default="", alias="NUTTI_MASCOT_IMAGE")
    # 생성된 프레임/영상을 저장하는 로컬 디렉터리(Veo 산출물은 48시간 후 삭제되므로 즉시 저장).
    nutti_media_dir: str = Field(default="data/media", alias="NUTTI_MEDIA_DIR")
    veo_poll_interval_sec: float = Field(default=15.0, alias="NUTTI_VEO_POLL_INTERVAL_SEC")
    veo_timeout_sec: float = Field(default=600.0, alias="NUTTI_VEO_TIMEOUT_SEC")

    # 영상 백엔드 선택:
    #   "veo"(기본) — Gemini API Veo, 네이티브 한국어 음성, 일일 쿼터 있음.
    #   "kling" — fal.ai Kling, 무음 영상 + 한국어 TTS 보이스오버.
    #   "veo_fal" — fal.ai 경유 Veo 3.1, 네이티브 한국어 음성 + 종량제(쿼터 벽 우회).
    #              Gemini Veo와 같은 모델이므로 품질·마스코트 일관성 동일, 단가는 모델별 분기.
    video_backend: Literal["veo", "kling", "veo_fal"] = Field(
        default="veo", alias="NUTTI_VIDEO_BACKEND"
    )
    # 2단계-Kling: fal.ai Kling image-to-video(무음). FAL_KEY는 fal.ai 대시보드에서 발급.
    fal_key: str = Field(default="", alias="FAL_KEY")
    # 기본 v2.1 standard — 무음·최저가($0.084/s)·5·10초 길이. v3는 duration 자유지만 가격 미확정.
    kling_model: str = Field(
        default="fal-ai/kling-video/v2.1/standard/image-to-video", alias="NUTTI_KLING_MODEL"
    )
    kling_poll_interval_sec: float = Field(default=10.0, alias="NUTTI_KLING_POLL_INTERVAL_SEC")
    kling_timeout_sec: float = Field(default=600.0, alias="NUTTI_KLING_TIMEOUT_SEC")
    # 2단계-TTS: 한국어 보이스오버. Gemini TTS는 기존 GEMINI_API_KEY를 그대로 쓴다(신규 키 불요).
    tts_model: str = Field(default="gemini-2.5-flash-preview-tts", alias="NUTTI_TTS_MODEL")
    # Gemini 사전구성 음성 이름(한국어 발화 지원). 예: Kore, Puck, Charon, Aoede 등.
    tts_voice: str = Field(default="Kore", alias="NUTTI_TTS_VOICE")

    # ElevenLabs TTS(한국어 아이 목소리) — Kling LipSync 후처리에서 아이 목소리로 재사용 예정.
    # ElevenLabs 전용 키 — api.elevenlabs.io에만 첨부(CDN 등 타 호스트 유출 금지).
    elevenlabs_api_key: str = Field(default="", alias="ELEVENLABS_API_KEY")
    elevenlabs_model_id: str = Field(
        default="eleven_multilingual_v2", alias="NUTTI_ELEVENLABS_MODEL"
    )
    # 프리메이드(영숫자) 또는 사용자 생성/클론(UUIDv4, 하이픈 포함) voice_id 모두 허용.
    elevenlabs_voice_id: str = Field(
        default="21m00Tcm4TlvDq8ikWAM", alias="NUTTI_ELEVENLABS_VOICE_ID"
    )

    # Supertone TTS(한국어 캐릭터 보이스, 2026-06-12 PO 선정) — kling_tts="supertone" 시 사용.
    # Supertone 전용 키 — supertoneapi.com에만 첨부(타 호스트 유출 금지).
    supertone_api_key: str = Field(default="", alias="SUPERTONE_API_KEY")
    # 콤마 구분 복수 voice_id — 영상 1편마다 대본 기반으로 결정적으로 1개 선택(로테이션).
    # 기본값 = PO가 고른 2종: Cheeky(건방진 마법사 소녀)·Aiko.
    supertone_voice_ids: str = Field(
        default="d40bae491c78a65f2f8488,ac449f240c2732b7f0b8bb",
        alias="NUTTI_SUPERTONE_VOICE_IDS",
    )
    # Supertone 합성 모델. sona_speech_2 = 플래그십(샘플 검증 완료).
    supertone_model: str = Field(default="sona_speech_2", alias="NUTTI_SUPERTONE_MODEL")

    # ---- Kling LipSync 후처리 ----
    # true면 무음 Kling 클립 생성 후 TTS 음성으로 fal.ai LipSync 처리한다.
    # false(기본)면 기존 보이스오버(mux) 동작을 그대로 유지한다.
    kling_lipsync: bool = Field(default=False, alias="NUTTI_KLING_LIPSYNC")
    # kling 백엔드 내레이션의 TTS 소스. "gemini"(기본, GEMINI_API_KEY 재사용) |
    # "elevenlabs"(ELEVENLABS_API_KEY 필요) | "supertone"(SUPERTONE_API_KEY 필요).
    kling_tts: Literal["gemini", "elevenlabs", "supertone"] = Field(
        default="gemini", alias="NUTTI_KLING_TTS"
    )
    # fal.ai Kling LipSync 모델 경로. 변경 시 _validate_model_id가 형식을 검증한다.
    kling_lipsync_model: str = Field(
        default="fal-ai/kling-video/lipsync/audio-to-video",
        alias="NUTTI_KLING_LIPSYNC_MODEL",
    )

    # ---- fal.ai FLUX.1 Kontext 프레임 생성(영상 백엔드 무관) ----
    # 영상 시작 프레임을 FLUX.1 Kontext [pro]로 생성한다. 마스코트 레퍼런스 이미지를
    # image_url로 넣고 의상·장소 프롬프트를 주면 마스코트를 유지한 채 편집한다.
    # FAL_KEY(기존 Kling/Veo fal 키)를 재사용한다 — 추가 키 불요.
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
    # Gemini API Veo와 동일한 모델을 fal.ai 종량제로 호스팅해 일일 쿼터 벽을 우회한다.
    # Lite 화질로 싸게 검증하고, Fast로 승격할 때는 모델명만 바꾼다(PO 승인 후).
    # FAL_KEY는 기존 kling 백엔드와 공유한다(fal.ai 단일 키).
    veo_fal_model: str = Field(
        default="fal-ai/veo3.1/lite/image-to-video",
        alias="NUTTI_VEO_FAL_MODEL",
    )
    # fal 큐 폴링 간격(초). Veo 생성이 Kling보다 오래 걸릴 수 있으므로 Kling보다 넉넉히.
    veo_fal_poll_interval_sec: float = Field(default=10.0, alias="NUTTI_VEO_FAL_POLL_INTERVAL_SEC")
    # fal 큐 전체 타임아웃(초). Veo 3.1은 최대 ~10분 소요를 대비해 Kling과 동일 한도.
    veo_fal_timeout_sec: float = Field(default=600.0, alias="NUTTI_VEO_FAL_TIMEOUT_SEC")
    # fal Veo 출력 해상도. "720p"(기본·저비용) | "1080p"(고품질).
    veo_fal_resolution: str = Field(default="720p", alias="NUTTI_VEO_FAL_RESOLUTION")

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
