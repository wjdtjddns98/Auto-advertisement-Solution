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

    # 1단계: 대본 (Claude)
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    script_model: str = Field(default="claude-opus-4-8", alias="NUTTI_SCRIPT_MODEL")
    # 실행 간 영속 상태(직전 성과 피드백·최근 주제) 저장 경로.
    # 매 사이클의 성과 분석을 다음 사이클 feedback으로 자동 연결하고,
    # 최근 주제를 기억해 주제 자동 생성 시 중복을 피하는 데 쓴다.
    state_path: str = Field(default="data/pipeline_state.json", alias="NUTTI_STATE_PATH")

    # 2단계: 영상 (Gemini 이미지 → Veo 3.1 image-to-video)
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_image_model: str = Field(
        default="gemini-2.5-flash-preview-05-20", alias="NUTTI_GEMINI_IMAGE_MODEL"
    )
    # 기본 Lite($0.05/초·720p): 2026-06-12 probe에서 한국어 발화·자막 없음 PO 합격.
    # Lite는 negativePrompt 미지원(VeoClient가 모델명으로 분기). 상향 시 fast/standard로.
    veo_model: str = Field(default="veo-3.1-lite-generate-preview", alias="NUTTI_VEO_MODEL")
    # 마스코트 레퍼런스 이미지 경로(없으면 텍스트 프롬프트만으로 시작 프레임 생성).
    nutti_mascot_image: str = Field(default="", alias="NUTTI_MASCOT_IMAGE")
    # 생성된 프레임/영상을 저장하는 로컬 디렉터리(Veo 산출물은 48시간 후 삭제되므로 즉시 저장).
    nutti_media_dir: str = Field(default="data/media", alias="NUTTI_MEDIA_DIR")
    veo_poll_interval_sec: float = Field(default=15.0, alias="NUTTI_VEO_POLL_INTERVAL_SEC")
    veo_timeout_sec: float = Field(default=600.0, alias="NUTTI_VEO_TIMEOUT_SEC")

    # 영상 백엔드 선택: "veo"(기본, 네이티브 한국어 음성) | "kling"(무음 영상 + 한국어 TTS 보이스오버).
    # Kling은 네이티브 한국어 음성이 불가(v3는 영어로 자동번역, v1.6/2.1은 무음)하므로,
    # 무음 영상을 생성하고 Gemini TTS로 한국어 내레이션을 별도 합성해 mux한다(립싱크 포기·보이스오버).
    video_backend: Literal["veo", "kling"] = Field(default="veo", alias="NUTTI_VIDEO_BACKEND")
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
