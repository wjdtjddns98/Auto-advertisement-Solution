"""CLI 실행 락 단위 테스트(동시 런 방지).

2026-07-29 실측 사고: 검수 게이트에서 대기 중인 런이 있는데 새 런을 띄워 두 프로세스가
같은 봇 토큰으로 getUpdates 롱폴을 걸었고, 텔레그램이 409 Conflict로 한쪽을 죽였다
(대본 카드까지 보낸 런이 통째로 소실).
"""

from __future__ import annotations

from nutti.cli import _acquire_run_lock
from nutti.config import Settings


def _settings(tmp_path, timeout: int = 3600) -> Settings:
    return Settings(
        NUTTI_ENV="test",
        NUTTI_STATE_PATH=str(tmp_path / "state.json"),
        NUTTI_REVIEW_TIMEOUT_SEC=str(timeout),
    )


def test_second_run_is_blocked_while_first_holds_lock(tmp_path):
    """락을 잡은 런이 살아 있으면 두 번째 실행은 None을 받아 거절된다."""
    settings = _settings(tmp_path)
    first = _acquire_run_lock(settings)

    assert first is not None
    assert first.exists()
    assert _acquire_run_lock(settings) is None


def test_lock_is_reusable_after_release(tmp_path):
    """락을 놓으면 다음 런이 정상적으로 잡는다(정상 종료 경로)."""
    settings = _settings(tmp_path)
    first = _acquire_run_lock(settings)
    assert first is not None
    first.unlink()

    second = _acquire_run_lock(settings)
    assert second is not None


def test_stale_lock_is_taken_over(tmp_path):
    """검수 대기 상한 + 여유를 넘긴 락은 죽은 런의 잔재로 보고 인수한다.

    안 그러면 크래시가 남긴 락 파일 하나로 이후 모든 런이 영구히 막힌다.
    """
    import os
    import time

    settings = _settings(tmp_path, timeout=1)  # 상한 1초 + 여유 1800초
    lock = _acquire_run_lock(settings)
    assert lock is not None
    old = time.time() - (1 + 1800 + 10)
    os.utime(lock, (old, old))

    assert _acquire_run_lock(settings) is not None
