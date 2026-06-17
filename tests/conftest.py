"""테스트 전역 격리 픽스처.

`.env`에 `NUTTI_DRY_RUN=false`와 실제 영상/외부 API 키가 설정돼 있어도, 테스트가
실수로 실제(유료) 네트워크 호출을 때리지 않도록 차단한다. 모든 라이브 경로 테스트는
fake 클라이언트 주입 또는 dry_run으로 동작하므로, 실제 소켓 전송이 발생하면 그것은
테스트 격리가 깨졌다는 신호다 — 조용히 과금되는 대신 명시적으로 실패시킨다.

httpx.Request/Response 객체 생성(예외 fixture 구성용)은 막지 않고, 실제 전송
(send/handle_request)만 차단한다.

## .env 격리가 필요한 이유

이 머신의 리포 `.env`에 `NUTTI_DRY_RUN=false` 및 실제 외부 API 키들이 설정돼 있으면,
Settings(pydantic-settings BaseSettings)가 `env_file='.env'`로 그 값을 읽어 들인다.
그 결과 override 없는 `Settings(...)`가 dry_run=False로 생성되어, 네트워크 차단 가드
(_block_real_network)가 발동하거나 키 검증 ValueError가 터진다 — 완전히 머신 의존적인 결함이다.

`_isolate_settings_env` 픽스처가 이를 막는다:
1. `Settings.model_config['env_file']`을 None으로 덮어 `.env` 파일 로딩을 끈다.
2. OS 환경변수(셸 export 등)에 남아 있을 수 있는 Nutti/외부 API 키들을 삭제한다.
3. `get_settings` lru_cache를 클리어해 캐시된 라이브 Settings가 재사용되지 않도록 한다.

결과: override 없는 `Settings(...)`는 코드 기본값(dry_run=True, video_backend='veo_fal')
으로 확정적으로 잡힌다. 각 테스트가 명시적으로 넘기는 kwargs는 생성자 인자(init)가
env_file보다 우선이므로 격리 후에도 그대로 적용된다.
"""

from __future__ import annotations

import pytest


class NetworkAccessBlockedError(RuntimeError):
    """테스트 중 실제 네트워크 전송이 시도됐을 때 발생(격리 위반 신호)."""


# 격리 대상 환경변수 목록 — Settings alias 기준 영상 파이프라인에 영향을 주는 것.
# OS에 export돼 있으면 env_file을 꺼도 Settings에 누수되므로 함께 제거한다.
# !! 규약: config.py에 Settings 필드를 추가할 때 해당 alias를 이 목록에도 반드시 동시에 추가 !!
_NUTTI_ENV_VARS: tuple[str, ...] = (
    "NUTTI_DRY_RUN",
    "NUTTI_VIDEO_BACKEND",
    "NUTTI_ENV",
    "NUTTI_LOG_LEVEL",
    "NUTTI_SCRIPT_MODEL",
    "NUTTI_VEO_FAL_MODEL",
    "NUTTI_VEO_FAL_POLL_INTERVAL_SEC",
    "NUTTI_VEO_FAL_TIMEOUT_SEC",
    "NUTTI_VEO_FAL_RESOLUTION",
    "NUTTI_KONTEXT_MODEL",
    "NUTTI_KONTEXT_POLL_INTERVAL_SEC",
    "NUTTI_KONTEXT_TIMEOUT_SEC",
    "NUTTI_MEDIA_DIR",
    "NUTTI_MASCOT_IMAGE",
    "NUTTI_STATE_PATH",
    "NUTTI_COST_LEDGER_PATH",
    "NUTTI_REVIEW_TIMEOUT_SEC",
    "NUTTI_REVIEW_POLL_INTERVAL_SEC",
    "NUTTI_REVIEW_STORE_PATH",
    "NUTTI_CALCULATOR_URL",
    "FAL_KEY",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DISCORD_WEBHOOK_URL",
    "GOOGLE_SHEETS_ID",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "YOUTUBE_CLIENT_ID",
    "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
    "NUTTI_YOUTUBE_PRIVACY_STATUS",
    "NUTTI_YOUTUBE_CATEGORY_ID",
    "NUTTI_YOUTUBE_DEFAULT_LANGUAGE",
    "NUTTI_YOUTUBE_MADE_FOR_KIDS",
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_ACCOUNT_ID",
)


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch):
    """Settings가 리포 `.env` 또는 OS 환경변수를 읽어 기본값이 뒤집히는 것을 막는다.

    이 픽스처가 없으면 머신의 `.env`(예: NUTTI_DRY_RUN=false, 실제 API 키들)가 테스트에
    누수돼 _block_real_network 가드가 발동하거나 키 검증 ValueError가 터진다.
    픽스처 종료 시 monkeypatch가 자동으로 원복한다.

    명시적 kwargs(예: Settings(NUTTI_DRY_RUN=False, GEMINI_API_KEY='test-key'))는
    생성자 인자(init source)가 env_file보다 우선이므로 격리 후에도 그대로 적용된다.
    """
    from nutti.config import Settings, get_settings

    # 1) .env 파일 로딩 비활성화 — model_config는 dict이므로 setitem으로 덮어쓴다.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    # 2) OS 환경변수 누수 차단 — 셸에 export돼 있어도 Settings에 들어가지 않도록.
    for var in _NUTTI_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # 3) get_settings() lru_cache 클리어 — 이전 테스트에서 캐시된 라이브 Settings가
    #    재사용되지 않도록 한다(캐시 히트로 격리가 우회되는 것 방지).
    get_settings.cache_clear()

    yield

    # teardown: yield 이후 monkeypatch가 자동 원복하지만, cache도 재클리어해
    # 다음 테스트가 오염된 캐시를 보지 않도록 한다.
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_cost_ledger(tmp_path, monkeypatch, _isolate_settings_env):
    """비용 원장이 리포지토리 data/cost_ledger.json을 오염시키지 않도록 tmp로 격리한다.

    _isolate_settings_env를 의존성으로 선언해 그 뒤에 실행되므로(env 삭제 후 set),
    명시 경로를 주입하지 않는 Orchestrator(예: test_cost 오케스트레이터 케이스)도
    tmp 원장을 쓴다. Settings(cost_ledger_path)는 이 env에서 읽힌다.
    """
    monkeypatch.setenv("NUTTI_COST_LEDGER_PATH", str(tmp_path / "cost_ledger.json"))


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """실제 httpx 전송(동기/비동기)을 차단한다 — fake 주입/dry_run만 허용.

    httpx import 자체가 실패하는 환경(미설치)에서는 막을 대상이 없으므로 건너뛴다.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx는 의존성에 포함
        return

    def _blocked(*_args, **_kwargs):
        raise NetworkAccessBlockedError(
            "테스트에서 실제 네트워크 전송이 시도됐습니다 — fake 클라이언트(http=) "
            "주입 또는 dry_run으로 격리해야 합니다."
        )

    # 실제 소켓을 여는 진입점만 막는다(Request/Response 생성은 그대로 허용).
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked, raising=False)
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", _blocked, raising=False
    )
