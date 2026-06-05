"""테스트 전역 격리 픽스처.

`.env`에 `NUTTI_DRY_RUN=false`와 실제 영상/외부 API 키가 설정돼 있어도, 테스트가
실수로 실제(유료) 네트워크 호출을 때리지 않도록 차단한다. 모든 라이브 경로 테스트는
fake 클라이언트 주입 또는 dry_run으로 동작하므로, 실제 소켓 전송이 발생하면 그것은
테스트 격리가 깨졌다는 신호다 — 조용히 과금되는 대신 명시적으로 실패시킨다.

httpx.Request/Response 객체 생성(예외 fixture 구성용)은 막지 않고, 실제 전송
(send/handle_request)만 차단한다.
"""

from __future__ import annotations

import pytest


class NetworkAccessBlockedError(RuntimeError):
    """테스트 중 실제 네트워크 전송이 시도됐을 때 발생(격리 위반 신호)."""


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
