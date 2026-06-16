"""Settings .env 격리(conftest `_isolate_settings_env`)의 회귀 핀 테스트.

conftest.py 안의 test 함수는 일반 실행(testpaths 수집)에서 수집되지 않으므로,
격리 픽스처의 동작 핀은 이 정식 테스트 파일에 둔다.
"""

from __future__ import annotations

from nutti.config import Settings


def test_settings_defaults_isolated_from_env():
    """격리 픽스처 하에서 override 없는 Settings()가 코드 기본값을 반환한다.

    리포 `.env`에 NUTTI_DRY_RUN=false가 설정돼 있어도 이 테스트가 통과해야 한다 —
    머신 의존성 검증 핀.
    """
    s = Settings()
    assert s.dry_run is True, "dry_run 기본값이 True여야 한다(env_file 누수 없음)"
    assert s.video_backend == "veo_fal", (
        "video_backend 기본값이 'veo_fal'이어야 한다(env_file 누수 없음)"
    )
    assert s.fal_key == "", "fal_key 기본값이 빈 문자열이어야 한다"
    assert s.anthropic_api_key == "", "anthropic_api_key 기본값이 빈 문자열이어야 한다"


def test_settings_explicit_kwargs_override_env_isolation():
    """pydantic-settings init-source 우선순위를 검증한다.

    생성자 kwargs(init source)는 env_file·OS environ보다 항상 우선이므로,
    Settings(NUTTI_DRY_RUN=False, FAL_KEY='k')는 격리 픽스처 유무와 관계없이
    그 값을 반환한다.

    참고: 이 테스트는 pydantic-settings의 init-source 계약을 문서화하며
    _live_settings 헬퍼의 동작 전제를 확인한다.
    격리 픽스처 자체의 회귀 핀은 test_settings_defaults_isolated_from_env 참조.
    """
    s = Settings(**{"NUTTI_DRY_RUN": False, "FAL_KEY": "my-key"})
    assert s.dry_run is False
    assert s.fal_key == "my-key"


def test_settings_veo_fal_backend_explicit_kwargs_works():
    """pydantic-settings init-source 우선순위(veo_fal 백엔드)를 검증한다.

    명시적 NUTTI_VIDEO_BACKEND='veo_fal'·FAL_KEY override는 격리 픽스처 유무와
    관계없이 적용된다 — 라이브 경로 테스트 헬퍼 계약 문서화.
    """
    s = Settings(
        **{
            "NUTTI_DRY_RUN": False,
            "NUTTI_VIDEO_BACKEND": "veo_fal",
            "FAL_KEY": "test-fal-key",
        }
    )
    assert s.video_backend == "veo_fal"
    assert s.dry_run is False
    assert s.fal_key == "test-fal-key"


def test_all_settings_aliases_in_isolation_list():
    """드리프트 가드 — Settings의 모든 필드 alias가 격리 목록에 들어 있어야 한다.

    config.py에 새 필드(예: ELEVENLABS_API_KEY)를 추가하면서 conftest의
    `_NUTTI_ENV_VARS`에 alias를 빠뜨리면, OS 환경변수로 export된 값이 테스트에
    누수돼 머신 의존 결함이 재발한다. 이 테스트가 그 누락을 즉시 잡는다.
    """
    from conftest import _NUTTI_ENV_VARS

    aliases = {(f.alias or name.upper()) for name, f in Settings.model_fields.items()}
    missing = aliases - set(_NUTTI_ENV_VARS)
    assert not missing, (
        f"Settings 필드 alias가 conftest._NUTTI_ENV_VARS 격리 목록에 없습니다: {sorted(missing)} "
        "— config.py에 필드를 추가했다면 격리 목록에도 같은 alias를 추가하세요."
    )
