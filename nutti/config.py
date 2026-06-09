"""환경설정. `.env` 파일 또는 환경변수에서 로드한다.

`NUTTI_DRY_RUN=true`(기본값)이면 외부 API 키 없이도 전 파이프라인이
시뮬레이션으로 동작하므로, 키를 채우기 전에 구조부터 검증할 수 있다.
"""

from __future__ import annotations

from functools import lru_cache

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
    veo_model: str = Field(default="veo-3.1-fast-generate-preview", alias="NUTTI_VEO_MODEL")
    # 마스코트 레퍼런스 이미지 경로(없으면 텍스트 프롬프트만으로 시작 프레임 생성).
    nutti_mascot_image: str = Field(default="", alias="NUTTI_MASCOT_IMAGE")
    # 생성된 프레임/영상을 저장하는 로컬 디렉터리(Veo 산출물은 48시간 후 삭제되므로 즉시 저장).
    nutti_media_dir: str = Field(default="data/media", alias="NUTTI_MEDIA_DIR")
    veo_poll_interval_sec: float = Field(default=15.0, alias="NUTTI_VEO_POLL_INTERVAL_SEC")
    veo_timeout_sec: float = Field(default=600.0, alias="NUTTI_VEO_TIMEOUT_SEC")

    # 영상 백엔드 선택: "veo"(기본, 네이티브 한국어 음성) | "kling"(무음 영상 + 한국어 TTS 보이스오버).
    # Kling은 네이티브 한국어 음성이 불가(v3는 영어로 자동번역, v1.6/2.1은 무음)하므로,
    # 무음 영상을 생성하고 Gemini TTS로 한국어 내레이션을 별도 합성해 mux한다(립싱크 포기·보이스오버).
    video_backend: str = Field(default="veo", alias="NUTTI_VIDEO_BACKEND")
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
