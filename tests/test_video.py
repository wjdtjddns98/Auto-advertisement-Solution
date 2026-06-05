"""VideoStudio 단위 테스트 — Hedra·Seedance/Kling·AssemblyAI·합성 연동.

모든 테스트는 fake 클라이언트 주입 또는 dry_run으로 **네트워크 없이** 동작한다.
워크스트림별 섹션(WS-A/B/C)으로 나누어 폴링·타임아웃·HTTP 에러 방어를 핀다.

shared_prep 단계에서는 골격(헬퍼·fake 클래스 stub·섹션 주석)만 제공하고,
실제 단언은 각 워크스트림(WS-A/B/C)이 자신의 섹션에 추가한다.
"""

from __future__ import annotations

import base64

import pytest

from nutti.config import Settings
from nutti.integrations.video import (
    AssemblyAIClient,
    SubtitleError,
    VideoRenderError,
    VideoStudio,
    VideoTimeoutError,
    _as_srt_data_url,
)
from nutti.models import Script


def _dry_settings(**overrides) -> Settings:
    """dry_run 환경 설정(네트워크/키 불요). 필요한 필드는 overrides로 덮어쓴다.

    Settings는 alias(NUTTI_DRY_RUN)로만 채워지므로 alias 키로 dry_run을 켠다.
    (필드명 `dry_run`로 넘기면 populate_by_name 미설정 탓에 무시되어
    .env의 NUTTI_DRY_RUN 값이 그대로 남는다.)
    """
    base: dict = {"NUTTI_DRY_RUN": True}
    base.update(overrides)
    return Settings(**base)


def _live_settings(**overrides) -> Settings:
    """실 경로(non-dry_run) 설정. 실제 호출은 fake 클라이언트 주입으로 차단한다.

    Settings는 alias(NUTTI_DRY_RUN)로만 채워지므로 alias 키로 dry_run을 끈다.
    (필드명 `dry_run`로 넘기면 populate_by_name 미설정 탓에 무시되어
    .env의 NUTTI_DRY_RUN=true가 그대로 남는다.)
    """
    base: dict = {"NUTTI_DRY_RUN": False}
    base.update(overrides)
    return Settings(**base)


def _script(topic: str = "강아지 간식", body: str = "본문") -> Script:
    """테스트용 최소 대본."""
    return Script(topic=topic, body=body)


def _no_sleep(_seconds):
    """폴링 대기 없이 즉시 반환하는 가짜 sleep(시간 결정성 확보).

    공용 헬퍼이므로 파일 상단에 둔다 — WS-A/B/C 모든 섹션이 전방 참조 없이 쓴다.
    """
    return None


# --- WS-A: Hedra ---


class FakeHedraClient:
    """Hedra Character-3 실 클라이언트 대체.

    VideoStudio가 기대하는 인터페이스(`render_character(*, text_prompt,
    character_id)`)를 구현한다. 호출 인자를 기록하고 결정적 URL을 반환한다.
    """

    def __init__(self, url: str = "https://fake.local/hedra/char.mp4"):
        self._url = url
        self.calls: list[tuple[str, str]] = []

    def render_character(self, *, text_prompt: str, character_id: str = "") -> str:
        self.calls.append((text_prompt, character_id))
        return self._url


def test_render_character_dry_run_returns_dummy_url():
    """dry_run이면 네트워크/주입 없이 결정적 더미 URL을 반환한다."""
    studio = VideoStudio(_dry_settings())
    script = _script()
    out = studio._render_character(script)
    assert out == f"https://dryrun.local/hedra/{script.id}.mp4"


def test_render_character_delegates_to_injected_client():
    """실 경로에서는 주입된 Hedra 클라이언트에 위임하고 URL을 그대로 반환한다."""
    fake = FakeHedraClient(url="https://fake.local/hedra/x.mp4")
    studio = VideoStudio(
        _live_settings(HEDRA_CHARACTER_ID="img-asset-1"),
        hedra_client=fake,
    )
    out = studio._render_character(_script(body="대본 본문"))
    assert out == "https://fake.local/hedra/x.mp4"
    # 대본 본문과 마스코트 이미지 asset id가 그대로 전달되어야 한다.
    assert fake.calls == [("대본 본문", "img-asset-1")]


# --- WS-A: HedraClient HTTP/폴링 방어 ---


class _HedraResp:
    """httpx.Response 대역(status_code + json만 흉내). Hedra는 status_code로 판정."""

    def __init__(self, *, status_code: int = 200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


class _HedraHttp:
    """주입용 httpx.Client 대역. post 1개, get 응답 큐를 순서대로 반환한다."""

    def __init__(self, *, post=None, get=None):
        self._post = post or _HedraResp(json_data={"id": "gen-1"})
        self._gets = list(get or [])
        self.get_calls = 0

    def post(self, *args, **kwargs):
        return self._post

    def get(self, *args, **kwargs):
        self.get_calls += 1
        # 큐가 비면 마지막 응답을 계속 반환(폴링 지속 시뮬레이션).
        if len(self._gets) > 1:
            return self._gets.pop(0)
        return self._gets[0] if self._gets else _HedraResp(json_data={})


def _hedra_client(http, *, timeout: float = 5.0):
    """HedraClient를 fake http/sleep로 생성하는 헬퍼(타임아웃 짧게)."""
    from nutti.integrations.video import HedraClient

    settings = _live_settings()
    object.__setattr__(settings, "hedra_poll_interval_sec", 0.5)
    object.__setattr__(settings, "hedra_timeout_sec", timeout)
    return HedraClient(settings, http=http, sleep=_no_sleep)


def test_hedra_client_completed_immediately_returns_url():
    """첫 폴링에서 complete면 download_url을 반환한다."""
    http = _HedraHttp(
        post=_HedraResp(json_data={"id": "gen-9"}),
        get=[_HedraResp(json_data={"status": "complete", "download_url": "https://h/a.mp4"})],
    )
    client = _hedra_client(http)
    assert client.render_character(text_prompt="t") == "https://h/a.mp4"


def test_hedra_client_url_fallback_when_no_download_url():
    """download_url이 없으면 url 필드로 폴백한다."""
    http = _HedraHttp(
        get=[_HedraResp(json_data={"status": "complete", "url": "https://h/b.mp4"})],
    )
    client = _hedra_client(http)
    assert client.render_character(text_prompt="t") == "https://h/b.mp4"


def test_hedra_client_polls_twice_then_completed():
    """processing 2회(가운데 finalizing 포함) 후 complete면 정상 반환."""
    http = _HedraHttp(
        get=[
            _HedraResp(json_data={"status": "processing"}),
            _HedraResp(json_data={"status": "finalizing"}),
            _HedraResp(json_data={"status": "complete", "url": "https://h/c.mp4"}),
        ],
    )
    client = _hedra_client(http)
    assert client.render_character(text_prompt="t") == "https://h/c.mp4"
    assert http.get_calls == 3


def test_hedra_client_timeout_raises_video_timeout_error():
    """complete가 끝내 안 오면 VideoTimeoutError를 던진다.

    interval=0.5, timeout=1.0이면 `elapsed < timeout` 경계상 정확히 2회 폴링해야
    한다(off-by-one 회귀 핀: `<=`였다면 deadline 너머 3회가 됨)."""
    http = _HedraHttp(get=[_HedraResp(json_data={"status": "processing"})])
    client = _hedra_client(http, timeout=1.0)
    with pytest.raises(VideoTimeoutError):
        client.render_character(text_prompt="t")
    assert http.get_calls == 2


def test_hedra_client_create_http_500_raises_render_error():
    """생성 요청 HTTP 500이면 VideoRenderError를 즉시 전파한다."""
    http = _HedraHttp(post=_HedraResp(status_code=500))
    client = _hedra_client(http)
    with pytest.raises(VideoRenderError):
        client.render_character(text_prompt="t")


def test_hedra_client_poll_http_500_raises_render_error():
    """폴링 단계 HTTP 5xx도 VideoRenderError로 전파된다."""
    http = _HedraHttp(get=[_HedraResp(status_code=503, json_data={})])
    client = _hedra_client(http)
    with pytest.raises(VideoRenderError):
        client.render_character(text_prompt="t")


def test_hedra_client_status_error_raises_render_error():
    """상태가 'error'면 error_message를 담아 VideoRenderError를 던진다."""
    http = _HedraHttp(
        get=[_HedraResp(json_data={"status": "error", "error_message": "bad input"})],
    )
    client = _hedra_client(http)
    with pytest.raises(VideoRenderError, match="bad input"):
        client.render_character(text_prompt="t")


def test_hedra_client_missing_generation_id_raises():
    """생성 응답에 id가 없으면 VideoRenderError."""
    http = _HedraHttp(post=_HedraResp(json_data={}))
    client = _hedra_client(http)
    with pytest.raises(VideoRenderError):
        client.render_character(text_prompt="t")


class _NoStatusResp:
    """status_code 속성이 없는 응답 대역(잘못 만든 fake/응답 시뮬레이션)."""

    def json(self):
        return {"id": "gen-1"}


def test_hedra_raise_for_status_rejects_missing_status_code():
    """status_code가 없는 응답은 200으로 가정해 조용히 통과시키지 않고 실패시킨다.

    과거에는 getattr(resp,'status_code',200) 기본값 탓에 잘못된 fake/응답이
    무음 통과했다. 이제 명시적으로 VideoRenderError를 던져야 한다(방어 강화)."""
    from nutti.integrations.video import HedraClient

    with pytest.raises(VideoRenderError, match="status_code"):
        HedraClient._raise_for_status(_NoStatusResp(), "Hedra 생성 요청")


def test_hedra_client_uses_x_api_key_header_not_bearer():
    """Hedra 인증은 X-API-Key 헤더(Bearer 아님)를 사용한다."""
    from nutti.integrations.video import HedraClient

    settings = _live_settings(HEDRA_API_KEY="sk_h_abc")
    client = HedraClient(settings, http=_HedraHttp(), sleep=_no_sleep)
    headers = client._headers
    assert headers["X-API-Key"] == "sk_h_abc"
    assert "Authorization" not in headers


# --- WS-B: Seedance/Kling ---
# pytest/VideoStudio/VideoRenderError/VideoTimeoutError는 파일 상단 공유 import를 재사용.

from nutti.integrations.video import (  # noqa: E402
    KlingClient,
    ScenesClientProtocol,
    SeedanceClient,
)


class _FakeScenesClient:
    """씬 클라이언트(Seedance/Kling) 공통 대역.

    `statuses`는 poll 호출마다 순서대로 반환할 URL(완료)/None(진행 중) 시퀀스다.
    큐가 한 개만 남으면 그 값을 계속 반환한다. submit 호출(프롬프트)을 기록한다.
    `submit_exc`/`poll_exc`를 주면 해당 호출에서 예외를 던져 HTTP 오류를 흉내 낸다.
    """

    def __init__(self, statuses=None, *, submit_exc=None, poll_exc=None):
        self._statuses = list(statuses or ["https://fake.local/scene.mp4"])
        self._submit_exc = submit_exc
        self._poll_exc = poll_exc
        self.submitted: list[str] = []
        self.poll_count = 0

    def submit(self, prompt: str) -> str:
        if self._submit_exc is not None:
            raise self._submit_exc
        self.submitted.append(prompt)
        return f"job-{len(self.submitted)}"

    def poll(self, job_id: str):
        self.poll_count += 1
        if self._poll_exc is not None:
            raise self._poll_exc
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]


class FakeSeedanceClient(_FakeScenesClient):
    """Seedance 2.0 실 클라이언트 대체(기본 화질 경로 검증용)."""


class FakeKlingClient(_FakeScenesClient):
    """Kling 3.0(고화질) 실 클라이언트 대체(고화질 경로 검증용)."""


def _b_no_sleep(_seconds):
    """폴링 대기 없이 즉시 반환하는 가짜 sleep."""
    return None


class _ScenesFakeResponse:
    """httpx.Response 대역(raise_for_status/json만 흉내)."""

    def __init__(self, *, json_data=None, raise_exc=None):
        self._json = json_data or {}
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        return self._json


class _ScenesFakeHttp:
    """주입용 httpx.Client 대역. request()로 미리 정한 응답을 순서대로 반환한다."""

    def __init__(self, request_results=None):
        self._results = list(request_results or [])

    def request(self, *args, **kwargs):
        return self._results.pop(0) if self._results else _ScenesFakeResponse()


# --- WS-B 테스트: _render_scenes ---


def test_scenes_dry_run_returns_two_dummy_urls():
    """dry_run이면 네트워크 없이 더미 씬 URL 2개를 반환한다."""
    studio = VideoStudio(_dry_settings())
    urls = studio._render_scenes(_script())
    assert len(urls) == 2
    assert all(u.startswith("https://dryrun.local/seedance/") for u in urls)


def test_scenes_injected_client_returns_url_list():
    """주입된 씬 클라이언트가 있으면 씬마다 URL을 모아 반환한다."""
    fake = FakeSeedanceClient(statuses=["https://fake.local/s.mp4"])
    studio = VideoStudio(_live_settings(), scenes_client=fake, sleep=_b_no_sleep)
    urls = studio._render_scenes(_script())
    # 프롬프트 개수만큼 submit 되고, 같은 개수의 URL이 나온다(현재 2개 씬).
    assert len(urls) == len(fake.submitted) == 2
    assert all(u == "https://fake.local/s.mp4" for u in urls)


def test_scenes_seedance_default_when_no_kling_key(monkeypatch):
    """kling_api_key가 비면 기본 Seedance 클라이언트를 생성한다."""
    created: list[str] = []

    def _fake_seedance(settings, *, sleep=None):
        created.append("seedance")
        return FakeSeedanceClient(statuses=["https://fake.local/seed.mp4"])

    def _fail_kling(*args, **kwargs):
        raise AssertionError("Kling이 선택되면 안 됨")

    monkeypatch.setattr("nutti.integrations.video.SeedanceClient", _fake_seedance)
    monkeypatch.setattr("nutti.integrations.video.KlingClient", _fail_kling)
    settings = _live_settings()
    # .env의 KLING_API_KEY가 로드될 수 있으므로 빈 값으로 강제(별칭 무시·결정성 확보).
    object.__setattr__(settings, "kling_api_key", "")
    studio = VideoStudio(settings, sleep=_b_no_sleep)
    urls = studio._render_scenes(_script())
    assert created == ["seedance"]
    assert all(u == "https://fake.local/seed.mp4" for u in urls)


def test_scenes_kling_highquality_when_key_present(monkeypatch):
    """kling_api_key가 있으면 고화질 Kling 클라이언트를 생성한다."""
    created: list[str] = []

    def _fake_kling(settings, *, sleep=None):
        created.append("kling")
        return FakeKlingClient(statuses=["https://fake.local/kling.mp4"])

    def _fail_seedance(*args, **kwargs):
        raise AssertionError("Seedance가 선택되면 안 됨")

    monkeypatch.setattr("nutti.integrations.video.KlingClient", _fake_kling)
    monkeypatch.setattr("nutti.integrations.video.SeedanceClient", _fail_seedance)
    settings = _live_settings()
    object.__setattr__(settings, "kling_api_key", "kl-123")  # 고화질 경로 강제.
    studio = VideoStudio(settings, sleep=_b_no_sleep)
    urls = studio._render_scenes(_script())
    assert created == ["kling"]
    assert all(u == "https://fake.local/kling.mp4" for u in urls)


def test_scenes_poll_delay_then_complete():
    """poll이 None(진행 중)을 거쳐 URL을 반환하면 정상 수집한다.

    씬1: None→None→URL(3폴) + 씬2: URL(1폴) = 총 4회 폴링.
    >= 3은 씬2 무한폴링 회귀(poll_count=54 등)를 통과시키므로 == 4로 결정적으로 핀다."""
    # 씬1: None→None→URL, 이후 씬2는 마지막 URL을 즉시 반환.
    fake = FakeSeedanceClient(statuses=[None, None, "https://fake.local/late.mp4"])
    studio = VideoStudio(_live_settings(), scenes_client=fake, sleep=_b_no_sleep)
    urls = studio._render_scenes(_script())
    assert urls[0] == "https://fake.local/late.mp4"
    assert fake.poll_count == 4


def test_scenes_timeout_raises_video_timeout_error():
    """제한 시간 안에 URL이 안 오면 VideoTimeoutError를 던진다.

    interval=0.5, timeout=1.0이면 `elapsed < timeout` 경계상 첫 씬에서 정확히
    2회 폴링하고 타임아웃해야 한다(off-by-one 회귀 핀)."""
    fake = FakeSeedanceClient(statuses=[None])  # 영원히 진행 중.
    settings = _live_settings()
    object.__setattr__(settings, "scene_timeout_sec", 1.0)
    object.__setattr__(settings, "scene_poll_interval_sec", 0.5)
    studio = VideoStudio(settings, scenes_client=fake, sleep=_b_no_sleep)
    with pytest.raises(VideoTimeoutError):
        studio._render_scenes(_script())
    # 첫 씬에서 2회 폴링 후 타임아웃(deadline 너머 추가 폴링 금지).
    assert fake.poll_count == 2


def test_scenes_submit_http_error_raises_render_error():
    """submit 단계 오류(HTTP 500 등)는 VideoRenderError로 전파된다."""
    fake = FakeSeedanceClient(submit_exc=VideoRenderError("씬 렌더 HTTP 500"))
    studio = VideoStudio(_live_settings(), scenes_client=fake, sleep=_b_no_sleep)
    with pytest.raises(VideoRenderError):
        studio._render_scenes(_script())


def test_seedance_client_submit_and_poll_with_fake_http():
    """SeedanceClient가 httpx 대역으로 submit→poll URL 흐름을 처리한다."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(json_data={"id": "cgt-1"}),  # submit
            _ScenesFakeResponse(
                json_data={
                    "status": "succeeded",
                    "content": {"video_url": "https://cdn/x.mp4"},
                }
            ),  # poll
        ]
    )
    client = SeedanceClient(_live_settings(), http=http)
    job_id = client.submit("프롬프트")
    assert job_id == "cgt-1"
    assert client.poll(job_id) == "https://cdn/x.mp4"


def test_seedance_client_poll_running_returns_none():
    """진행 중(running) 상태면 poll이 None을 반환한다."""
    http = _ScenesFakeHttp(
        request_results=[_ScenesFakeResponse(json_data={"status": "running"})]
    )
    client = SeedanceClient(_live_settings(), http=http)
    assert client.poll("cgt-1") is None


def test_seedance_client_poll_failed_raises_render_error():
    """실패 상태(failed)면 VideoRenderError로 전파한다."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(json_data={"status": "failed", "error": {"message": "x"}})
        ]
    )
    client = SeedanceClient(_live_settings(), http=http)
    with pytest.raises(VideoRenderError):
        client.poll("cgt-1")


def test_seedance_client_http_status_error_raises_render_error():
    """HTTP 5xx 응답은 VideoRenderError로 승격된다."""
    import httpx

    request = httpx.Request("POST", "https://x")
    response = httpx.Response(500, request=request)
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(
                raise_exc=httpx.HTTPStatusError("500", request=request, response=response)
            )
        ]
    )
    client = SeedanceClient(_live_settings(), http=http)
    with pytest.raises(VideoRenderError):
        client.submit("프롬프트")


def test_kling_client_submit_and_poll_with_fake_http():
    """KlingClient가 code/task_status 구조를 방어적으로 파싱한다."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(json_data={"code": 0, "data": {"task_id": "kt-1"}}),
            _ScenesFakeResponse(
                json_data={
                    "code": 0,
                    "data": {
                        "task_status": "succeed",
                        "task_result": {"videos": [{"url": "https://cdn/k.mp4"}]},
                    },
                }
            ),
        ]
    )
    client = KlingClient(_live_settings(kling_api_key="kl"), http=http)
    job_id = client.submit("프롬프트")
    assert job_id == "kt-1"
    assert client.poll(job_id) == "https://cdn/k.mp4"


def test_kling_client_nonzero_code_raises_render_error():
    """Kling이 code != 0(논리 오류)을 반환하면 VideoRenderError."""
    http = _ScenesFakeHttp(
        request_results=[_ScenesFakeResponse(json_data={"code": 1, "message": "bad"})]
    )
    client = KlingClient(_live_settings(kling_api_key="kl"), http=http)
    with pytest.raises(VideoRenderError):
        client.submit("프롬프트")


def test_kling_client_poll_failed_status_raises_render_error():
    """Kling poll이 task_status='failed'면 VideoRenderError로 전파한다(미커버 경로)."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(
                json_data={
                    "code": 0,
                    "data": {"task_status": "failed", "task_status_msg": "렌더 실패"},
                }
            )
        ]
    )
    client = KlingClient(_live_settings(kling_api_key="kl"), http=http)
    with pytest.raises(VideoRenderError, match="렌더 실패"):
        client.poll("kt-1")


def test_kling_client_poll_in_progress_returns_none():
    """Kling poll이 진행 중(processing/submitted)이면 None을 반환한다(미커버 경로)."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(json_data={"code": 0, "data": {"task_status": "processing"}})
        ]
    )
    client = KlingClient(_live_settings(kling_api_key="kl"), http=http)
    assert client.poll("kt-1") is None


def test_kling_client_poll_succeed_without_url_raises():
    """Kling이 succeed인데 videos URL이 없으면 방어적으로 VideoRenderError."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(
                json_data={
                    "code": 0,
                    "data": {"task_status": "succeed", "task_result": {"videos": []}},
                }
            )
        ]
    )
    client = KlingClient(_live_settings(kling_api_key="kl"), http=http)
    with pytest.raises(VideoRenderError):
        client.poll("kt-1")


def test_seedance_client_poll_succeeded_without_url_raises():
    """Seedance가 succeeded인데 video_url이 없으면 방어적으로 VideoRenderError(미커버)."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(json_data={"status": "succeeded", "content": {}})
        ]
    )
    client = SeedanceClient(_live_settings(), http=http)
    with pytest.raises(VideoRenderError, match="video_url"):
        client.poll("cgt-1")


def test_kling_client_direct_http_raises_not_implemented():
    """JWT 미구현이므로 주입 없이 실 HTTP를 시도하면 NotImplementedError로 막힌다.

    raw kling_api_key를 Bearer로 보내면 운영에서 401로 무음 실패하기 때문에,
    미리 서명된 토큰을 쓰는 http=가 주입되지 않으면 분명히 실패해야 한다.
    """
    client = KlingClient(_live_settings(kling_api_key="kl"))  # http 미주입
    with pytest.raises(NotImplementedError, match="JWT"):
        client.submit("프롬프트")


def test_request_json_wraps_non_httpx_transport_error():
    """_request_json은 httpx 외 stdlib 전송 오류(ConnectionError)도 감싼다.

    SeedanceClient/KlingClient가 공유하는 헬퍼라, 비-httpx 예외가 raw로 새어
    오케스트레이터를 깨뜨리지 않고 VideoRenderError로 통일돼야 한다.
    """

    class _BoomHttp:
        def request(self, *args, **kwargs):
            raise ConnectionError("network down")

    client = SeedanceClient(_live_settings(), http=_BoomHttp())
    with pytest.raises(VideoRenderError):
        client.submit("프롬프트")


def test_scenes_client_protocol_is_referenceable():
    """ScenesClientProtocol이 import 가능하고 fake가 인터페이스를 만족한다."""
    assert hasattr(ScenesClientProtocol, "submit")
    fake = FakeSeedanceClient()
    assert callable(fake.submit) and callable(fake.poll)


# --- WS-C: AssemblyAI + Compose ---


class FakeAssemblyAIClient:
    """AssemblyAI 전사 실 클라이언트 대체.

    `statuses` 큐로 poll 시 반환할 상태를 순서대로 흉내 낸다. 큐가 비면
    마지막 상태를 계속 반환한다. submit/fetch_srt 호출 횟수를 기록해 검증한다.
    """

    def __init__(self, statuses=None, srt="1\n00:00:00,000 --> 00:00:01,000\n안녕\n"):
        self._statuses = list(statuses or ["completed"])
        self._srt = srt
        self.submitted: list[str] = []
        self.fetched: list[str] = []
        self.poll_count = 0  # off-by-one 회귀를 잡기 위한 폴링 호출 횟수.

    def submit(self, audio_url: str) -> str:
        self.submitted.append(audio_url)
        return "transcript-123"

    def poll(self, transcript_id: str) -> str:
        self.poll_count += 1
        # 큐에 다음 상태가 있으면 꺼내고, 비면 마지막 상태를 유지한다.
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    def fetch_srt(self, transcript_id: str) -> str:
        self.fetched.append(transcript_id)
        return self._srt


class FakeComposer:
    """합성기 대체. compose 인자를 기록하고 결정적 URL 튜플을 반환한다."""

    def __init__(self):
        self.calls: list[tuple] = []

    def compose(self, character_url, scene_urls, subtitle_url):
        self.calls.append((character_url, scene_urls, subtitle_url))
        return ("https://fake.local/final.mp4", "https://fake.local/preview.gif")


# 참고: 공용 _no_sleep은 파일 상단에 정의돼 있다(전방 참조 제거).


# --- WS-C 테스트: AssemblyAI 자막 ---


def test_subtitles_dry_run_returns_srt_url():
    """dry_run이면 네트워크 없이 .srt 더미 URL을 반환한다."""
    studio = VideoStudio(_dry_settings())
    out = studio._generate_subtitles("https://x.local/clip.mp4")
    assert out == "https://x.local/clip.srt"


def test_subtitles_completed_immediately_returns_data_url():
    """폴링이 즉시 completed면 SRT를 data-URL로 감싸 반환한다."""
    fake = FakeAssemblyAIClient(statuses=["completed"])
    studio = VideoStudio(_live_settings(), assemblyai_client=fake, sleep=_no_sleep)
    out = studio._generate_subtitles("https://x.local/clip.mp4")
    assert out.startswith("data:application/x-subrip;base64,")
    decoded = base64.b64decode(out.split(",", 1)[1]).decode("utf-8")
    assert "안녕" in decoded
    assert fake.submitted == ["https://x.local/clip.mp4"]
    assert fake.fetched == ["transcript-123"]


def test_subtitles_poll_twice_then_completed():
    """processing 2회 후 completed면 정상적으로 자막을 반환한다."""
    fake = FakeAssemblyAIClient(statuses=["processing", "processing", "completed"])
    studio = VideoStudio(_live_settings(), assemblyai_client=fake, sleep=_no_sleep)
    out = studio._generate_subtitles("https://x.local/clip.mp4")
    assert out.startswith("data:application/x-subrip;base64,")


def test_subtitles_error_status_raises_subtitle_error():
    """상태가 error면 SubtitleError를 전파한다."""
    fake = FakeAssemblyAIClient(statuses=["processing", "error"])
    studio = VideoStudio(_live_settings(), assemblyai_client=fake, sleep=_no_sleep)
    with pytest.raises(SubtitleError):
        studio._generate_subtitles("https://x.local/clip.mp4")


def test_subtitles_timeout_raises_video_timeout_error():
    """completed가 끝까지 안 오면 VideoTimeoutError를 던진다."""
    fake = FakeAssemblyAIClient(statuses=["processing"])
    settings = _live_settings()
    # 폴링 한도를 작게 잡아 빠르게 타임아웃에 도달시킨다.
    object.__setattr__(settings, "subtitle_timeout_sec", 1.0)
    object.__setattr__(settings, "subtitle_poll_interval_sec", 0.5)
    studio = VideoStudio(settings, assemblyai_client=fake, sleep=_no_sleep)
    with pytest.raises(VideoTimeoutError):
        studio._generate_subtitles("https://x.local/clip.mp4")
    # interval=0.5, timeout=1.0이면 정확히 2회 폴링 후 타임아웃(off-by-one 회귀 핀).
    assert fake.poll_count == 2


def test_as_srt_data_url_roundtrip():
    """_as_srt_data_url은 base64 data-URL로 SRT를 왕복 보존한다."""
    srt = "1\n00:00:00,000 --> 00:00:02,000\nhello\n"
    url = _as_srt_data_url(srt)
    assert url.startswith("data:application/x-subrip;base64,")
    assert base64.b64decode(url.split(",", 1)[1]).decode("utf-8") == srt


# --- WS-C: AssemblyAIClient HTTP 방어 ---


class _FakeResponse:
    """httpx.Response 대역(상태/JSON/text/raise_for_status만 흉내)."""

    def __init__(self, *, json_data=None, text="", raise_exc=None):
        self._json = json_data or {}
        self.text = text
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        return self._json


class _FakeHttp:
    """주입용 httpx.Client 대역. 미리 정한 응답을 순서대로 반환한다."""

    def __init__(self, post=None, get=None):
        self._post = post
        self._gets = list(get or [])

    def post(self, *args, **kwargs):
        return self._post

    def get(self, *args, **kwargs):
        return self._gets.pop(0) if self._gets else _FakeResponse(json_data={})


def test_assemblyai_client_submit_http_error_raises_render_error():
    """제출 단계 HTTP 오류는 VideoRenderError로 승격된다."""
    http = _FakeHttp(post=_FakeResponse(raise_exc=RuntimeError("boom 500")))
    client = AssemblyAIClient("k", http=http)
    with pytest.raises(VideoRenderError):
        client.submit("https://x.local/clip.mp4")


def test_assemblyai_client_submit_missing_id_raises():
    """응답에 transcript id가 없으면 VideoRenderError."""
    http = _FakeHttp(post=_FakeResponse(json_data={}))
    client = AssemblyAIClient("k", http=http)
    with pytest.raises(VideoRenderError):
        client.submit("https://x.local/clip.mp4")


def test_assemblyai_client_poll_returns_status():
    """폴링은 status 문자열을 그대로 반환한다."""
    http = _FakeHttp(get=[_FakeResponse(json_data={"status": "processing"})])
    client = AssemblyAIClient("k", http=http)
    assert client.poll("t1") == "processing"


def test_assemblyai_client_fetch_srt_returns_text():
    """SRT 다운로드는 plain-text 본문을 반환한다."""
    http = _FakeHttp(get=[_FakeResponse(text="1\n00:00:00,000 --> 00:00:01,000\nhi\n")])
    client = AssemblyAIClient("k", http=http)
    assert "hi" in client.fetch_srt("t1")


def test_assemblyai_client_no_bearer_prefix_in_header():
    """AssemblyAI 인증 헤더는 Bearer 접두사 없이 키 값만 넣는다."""
    client = AssemblyAIClient("my-key")
    assert client._headers() == {"Authorization": "my-key"}


def test_assemblyai_client_poll_http_error_raises_render_error():
    """폴링 단계 HTTP/전송 오류도 VideoRenderError로 승격된다(회귀 방지)."""
    http = _FakeHttp(get=[_FakeResponse(raise_exc=RuntimeError("boom 500"))])
    client = AssemblyAIClient("k", http=http)
    with pytest.raises(VideoRenderError):
        client.poll("t1")


def test_assemblyai_client_fetch_srt_http_error_raises_render_error():
    """SRT 다운로드 단계 HTTP/전송 오류도 VideoRenderError로 승격된다(회귀 방지)."""
    http = _FakeHttp(get=[_FakeResponse(raise_exc=RuntimeError("boom 500"))])
    client = AssemblyAIClient("k", http=http)
    with pytest.raises(VideoRenderError):
        client.fetch_srt("t1")


def test_assemblyai_error_message_redacts_request_url():
    """HTTPStatusError 발생 시 에러 메시지에 요청 URL을 노출하지 않고 상태 코드만 남긴다.

    httpx 예외 문자열에는 transcript_id가 박힌 전체 URL이 들어가므로, 그대로
    메시지에 끼우면 정보 노출이 된다. redact 후 상태 코드만 보여야 한다."""
    import httpx

    secret_url = "https://api.assemblyai.com/v2/transcript/SECRET-ID-12345"
    request = httpx.Request("GET", secret_url)
    response = httpx.Response(403, request=request)
    exc = httpx.HTTPStatusError("forbidden", request=request, response=response)
    http = _FakeHttp(get=[_FakeResponse(raise_exc=exc)])
    client = AssemblyAIClient("k", http=http)
    with pytest.raises(VideoRenderError) as info:
        client.poll("SECRET-ID-12345")
    message = str(info.value)
    assert "403" in message
    assert "SECRET-ID-12345" not in message
    assert "api.assemblyai.com" not in message


def test_request_json_error_message_redacts_url():
    """_request_json도 HTTP 오류 메시지에 요청 URL을 노출하지 않는다(상태 코드만)."""
    import httpx

    secret_url = "https://ark.example/api/v3/tasks/SECRET-JOB-9"
    request = httpx.Request("GET", secret_url)
    response = httpx.Response(500, request=request)
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(
                raise_exc=httpx.HTTPStatusError("500", request=request, response=response)
            )
        ]
    )
    client = SeedanceClient(_live_settings(), http=http)
    with pytest.raises(VideoRenderError) as info:
        client.poll("SECRET-JOB-9")
    message = str(info.value)
    assert "500" in message
    assert "SECRET-JOB-9" not in message
    assert "ark.example" not in message


# --- 응답 본문 redaction 회귀 핀 ---


def test_hedra_missing_id_message_contains_keys_not_body():
    """Hedra 생성 응답에 id가 없을 때 에러 메시지에 원본 본문 대신 키 목록만 들어간다.

    응답 본문에는 서명된 CDN URL·토큰·내부 메타데이터가 포함될 수 있어,
    `{data!r}` 전체를 메시지에 끼우면 로그/텔레그램으로 유출된다.
    키 목록은 포함되고 비밀값(서명 토큰 등)은 포함되지 않아야 한다."""
    http = _HedraHttp(post=_HedraResp(json_data={"signed_token": "SECRET-TOKEN-ABC", "meta": "x"}))
    client = _hedra_client(http)
    with pytest.raises(VideoRenderError) as info:
        client.render_character(text_prompt="t")
    message = str(info.value)
    # 키 이름은 노출돼도 됨(진단 목적).
    assert "signed_token" in message or "meta" in message
    # 비밀값 자체는 메시지에 있으면 안 됨.
    assert "SECRET-TOKEN-ABC" not in message


def test_hedra_missing_url_message_contains_keys_not_body():
    """Hedra 완료 응답에 URL이 없을 때 에러 메시지에 키 목록만 들어간다."""
    http = _HedraHttp(
        get=[_HedraResp(json_data={"status": "complete", "signed_cdn_url": "https://cdn/SECRET"})]
    )
    client = _hedra_client(http)
    with pytest.raises(VideoRenderError) as info:
        client.render_character(text_prompt="t")
    message = str(info.value)
    assert "signed_cdn_url" in message or "status" in message
    assert "https://cdn/SECRET" not in message


def test_seedance_missing_id_message_contains_keys_not_body():
    """Seedance submit 응답에 id가 없을 때 에러 메시지에 키 목록만 들어간다."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(json_data={"internal_token": "SECRET-SEED-TOK", "code": 0})
        ]
    )
    client = SeedanceClient(_live_settings(), http=http)
    with pytest.raises(VideoRenderError) as info:
        client.submit("프롬프트")
    message = str(info.value)
    assert "internal_token" in message or "code" in message
    assert "SECRET-SEED-TOK" not in message


def test_seedance_missing_url_message_contains_keys_not_body():
    """Seedance poll 완료 시 video_url 없으면 키 목록만 노출한다."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(
                json_data={"status": "succeeded", "internal_signed_url": "https://cdn/SECRET2"}
            )
        ]
    )
    client = SeedanceClient(_live_settings(), http=http)
    with pytest.raises(VideoRenderError) as info:
        client.poll("cgt-1")
    message = str(info.value)
    assert "status" in message or "internal_signed_url" in message
    assert "https://cdn/SECRET2" not in message


def test_seedance_failed_status_message_contains_status_not_body():
    """Seedance 작업 실패 시 에러 메시지에 status/error_code만 포함되고 본문 전체는 없다."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(
                json_data={
                    "status": "failed",
                    "error": {"code": "E123", "secret_detail": "SECRET-DETAIL"},
                }
            )
        ]
    )
    client = SeedanceClient(_live_settings(), http=http)
    with pytest.raises(VideoRenderError) as info:
        client.poll("cgt-1")
    message = str(info.value)
    # status와 error code는 포함돼야 함.
    assert "failed" in message
    assert "E123" in message
    # 원본 본문의 비밀 상세값은 없어야 함.
    assert "SECRET-DETAIL" not in message


def test_kling_missing_task_id_message_contains_keys_not_body():
    """Kling submit 응답에 task_id 없을 때 키 목록만 노출한다."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(
                json_data={"code": 0, "data": {"internal_token": "SECRET-KLING-TOK"}}
            )
        ]
    )
    client = KlingClient(_live_settings(kling_api_key="kl"), http=http)
    with pytest.raises(VideoRenderError) as info:
        client.submit("프롬프트")
    message = str(info.value)
    assert "internal_token" in message or "data" in message
    assert "SECRET-KLING-TOK" not in message


def test_kling_missing_url_message_contains_keys_not_body():
    """Kling poll 완료 시 URL 없으면 키 목록만 노출한다."""
    http = _ScenesFakeHttp(
        request_results=[
            _ScenesFakeResponse(
                json_data={
                    "code": 0,
                    "data": {
                        "task_status": "succeed",
                        "task_result": {"videos": [], "secret_field": "SECRET-KLING-VAL"},
                    },
                }
            )
        ]
    )
    client = KlingClient(_live_settings(kling_api_key="kl"), http=http)
    with pytest.raises(VideoRenderError) as info:
        client.poll("kt-1")
    message = str(info.value)
    assert "data" in message or "code" in message
    assert "SECRET-KLING-VAL" not in message


# --- WS-C 테스트: 합성(_compose) ---


def test_compose_dry_run_returns_dummy_urls():
    """dry_run이면 캐릭터 URL에서 파생한 더미 final/preview를 반환한다.

    'hedra'→'final' 치환과 .mp4→_preview.gif 파생을 정확한 문자열로 핀다
    (trivial substring 단언 강화)."""
    studio = VideoStudio(_dry_settings())
    final, preview = studio._compose(
        "https://dryrun.local/hedra/abc.mp4", [], "sub.srt"
    )
    assert final == "https://dryrun.local/final/abc.mp4"
    assert preview == "https://dryrun.local/final/abc_preview.gif"


def test_compose_delegates_to_injected_composer():
    """주입된 합성기가 있으면 그쪽에 위임하고 결과를 반환한다."""
    fake = FakeComposer()
    studio = VideoStudio(_live_settings(), composer_client=fake)
    final, preview = studio._compose("char.mp4", ["s1.mp4", "s2.mp4"], "sub.srt")
    assert final == "https://fake.local/final.mp4"
    assert preview == "https://fake.local/preview.gif"
    assert fake.calls == [("char.mp4", ["s1.mp4", "s2.mp4"], "sub.srt")]


def test_base_composer_raises_not_implemented():
    """BaseComposer는 손상된 파생 URL 대신 NotImplementedError로 분명히 실패한다.

    과거에는 character_url.replace('hedra','final')로 가짜 URL을 만들었는데,
    실제 Hedra 응답 URL(예: https://h/a.mp4)에는 'hedra'가 없어 원본을 그대로
    돌려주는 무음 손상이 있었다. 이제 스토리지 연동 전까지 명시적으로 실패한다.
    """
    from nutti.integrations.video import BaseComposer

    composer = BaseComposer()
    with pytest.raises(NotImplementedError):
        composer.compose("https://h/a.mp4", ["s1.mp4"], "sub.srt")


def test_compose_base_composer_does_not_mangle_real_url():
    """주입 없는 실 경로 _compose는 손상 URL을 반환하지 않고 즉시 실패한다."""
    studio = VideoStudio(_live_settings())  # composer 미주입 → BaseComposer
    with pytest.raises(NotImplementedError):
        studio._compose("https://h/a.mp4", ["s1.mp4"], "sub.srt")


# --- WS-C 통합: produce() end-to-end (fake 주입) ---


class _AllFakeScenes:
    """produce 통합용 씬 클라이언트 대역(submit→poll 즉시 URL)."""

    def submit(self, prompt: str) -> str:
        return "job-1"

    def poll(self, job_id: str) -> str:
        return "https://fake.local/scene.mp4"


class _AllFakeHedra:
    """produce 통합용 캐릭터 클라이언트 대역."""

    def render_character(self, *, text_prompt: str, character_id: str) -> str:
        return "https://fake.local/hedra/char.mp4"


def test_produce_end_to_end_with_fakes_fills_all_fields():
    """fake 전부 주입 시 VideoAsset의 모든 URL 필드가 채워진다(네트워크 없음)."""
    studio = VideoStudio(
        _live_settings(),
        hedra_client=_AllFakeHedra(),
        scenes_client=_AllFakeScenes(),
        assemblyai_client=FakeAssemblyAIClient(statuses=["completed"]),
        composer_client=FakeComposer(),
        sleep=_no_sleep,
    )
    asset = studio.produce(_script())
    assert asset.script_id
    assert asset.character_clip_url == "https://fake.local/hedra/char.mp4"
    # 현재 _scene_prompts는 도입/마무리 2씬을 만들므로 정확히 2개여야 한다.
    assert len(asset.scene_clip_urls) == 2
    assert all(u == "https://fake.local/scene.mp4" for u in asset.scene_clip_urls)
    assert asset.subtitle_url.startswith("data:application/x-subrip;base64,")
    assert asset.final_url == "https://fake.local/final.mp4"
    assert asset.preview_url == "https://fake.local/preview.gif"


# --- 실 경로 사전 설정 검증(validate_config) ---


def _empty_live_settings(**overrides) -> Settings:
    """실 경로 설정에서 영상 관련 키를 모두 비워 검증 경로를 결정적으로 만든다.

    .env에 키가 채워져 있어도 테스트가 환경에 의존하지 않도록 강제로 비운다.
    """
    settings = _live_settings(**overrides)
    for field in ("hedra_api_key", "seedance_api_key", "kling_api_key", "assemblyai_api_key"):
        object.__setattr__(settings, field, "")
    return settings


def test_validate_config_dry_run_passes_without_keys():
    """dry_run이면 키가 모두 비어 있어도 검증을 통과한다."""
    VideoStudio(_dry_settings()).validate_config()  # 예외 없이 통과


def test_validate_config_missing_hedra_key_raises():
    """실 경로에서 HEDRA_API_KEY가 비면 시작 시점에 ValueError로 빠르게 실패한다."""
    studio = VideoStudio(_empty_live_settings())
    with pytest.raises(ValueError, match="HEDRA_API_KEY"):
        studio.validate_config()


def test_validate_config_missing_scene_keys_raises():
    """Hedra만 있고 Seedance/Kling 키가 모두 비면 ValueError."""
    settings = _empty_live_settings()
    object.__setattr__(settings, "hedra_api_key", "h")
    studio = VideoStudio(settings)
    with pytest.raises(ValueError, match="SEEDANCE_API_KEY"):
        studio.validate_config()


def test_validate_config_missing_assemblyai_key_raises():
    """Hedra·Seedance는 있으나 AssemblyAI 키가 비면 ValueError."""
    settings = _empty_live_settings()
    object.__setattr__(settings, "hedra_api_key", "h")
    object.__setattr__(settings, "seedance_api_key", "s")
    studio = VideoStudio(settings)
    with pytest.raises(ValueError, match="ASSEMBLYAI_API_KEY"):
        studio.validate_config()


def test_validate_config_injected_clients_skip_key_checks():
    """클라이언트를 주입하면 해당 키 검사를 건너뛰어 통과한다(테스트/대체 구현 허용)."""
    studio = VideoStudio(
        _empty_live_settings(),
        hedra_client=_AllFakeHedra(),
        scenes_client=_AllFakeScenes(),
        assemblyai_client=FakeAssemblyAIClient(),
    )
    studio.validate_config()  # 예외 없이 통과


def test_produce_fast_fails_on_missing_key_before_network():
    """produce()는 키 미설정 시 네트워크 호출 전에 ValueError로 빠르게 실패한다."""
    studio = VideoStudio(_empty_live_settings())
    with pytest.raises(ValueError, match="HEDRA_API_KEY"):
        studio.produce(_script())


def test_validate_config_only_kling_key_fast_fails():
    """Kling 키만 있고 Seedance가 비면, 렌더 도중 NotImplementedError 대신
    시작 시점에 ValueError로 빠르게 실패해야 한다(fast-fail 계약)."""
    settings = _empty_live_settings()
    object.__setattr__(settings, "hedra_api_key", "h")
    object.__setattr__(settings, "assemblyai_api_key", "a")
    object.__setattr__(settings, "kling_api_key", "kl-123")  # Seedance는 빈 채로.
    studio = VideoStudio(settings)
    with pytest.raises(ValueError, match="KLING_API_KEY"):
        studio.validate_config()


def test_validate_config_ignores_comment_value_in_kling_key():
    """.env 인라인 주석이 값으로 파싱돼도(`KLING_API_KEY=  # 설명`) Kling 키를
    진짜 키로 오인하지 않고, Seedance 누락을 정상적으로 fast-fail해야 한다."""
    settings = _empty_live_settings()
    object.__setattr__(settings, "hedra_api_key", "h")
    object.__setattr__(settings, "assemblyai_api_key", "a")
    # pydantic-settings가 빈 값+인라인 주석을 주석 문자열로 파싱한 상황 재현.
    object.__setattr__(settings, "kling_api_key", "# Kling 3.0 (고화질 옵션, 설명)")
    studio = VideoStudio(settings)
    # 주석 값은 무시되므로 Kling이 아니라 Seedance 누락으로 실패해야 한다.
    with pytest.raises(ValueError, match="SEEDANCE_API_KEY"):
        studio.validate_config()


def test_validate_config_ignores_comment_value_in_seedance_key():
    """Seedance 키 자리에 주석 문자열만 있으면 빈 키로 취급해 fast-fail한다."""
    settings = _empty_live_settings()
    object.__setattr__(settings, "hedra_api_key", "h")
    object.__setattr__(settings, "assemblyai_api_key", "a")
    object.__setattr__(settings, "seedance_api_key", "   # Seedance 키 설명")
    studio = VideoStudio(settings)
    with pytest.raises(ValueError, match="SEEDANCE_API_KEY"):
        studio.validate_config()


def test_build_scenes_client_ignores_comment_kling_key(monkeypatch):
    """_build_scenes_client도 주석 값을 Kling 키로 오인하지 않고 Seedance를 만든다."""
    created: list[str] = []

    def _fake_seedance(settings, *, sleep=None):
        created.append("seedance")
        return FakeSeedanceClient(statuses=["https://fake.local/seed.mp4"])

    def _fail_kling(*args, **kwargs):
        raise AssertionError("주석 값으로 Kling이 선택되면 안 됨")

    monkeypatch.setattr("nutti.integrations.video.SeedanceClient", _fake_seedance)
    monkeypatch.setattr("nutti.integrations.video.KlingClient", _fail_kling)
    settings = _live_settings()
    object.__setattr__(settings, "kling_api_key", "# 주석만 있는 값")
    studio = VideoStudio(settings, sleep=_b_no_sleep)
    studio._render_scenes(_script())
    assert created == ["seedance"]


# --- 회귀 핀: Hedra 캐릭터 필드명 / Kling _headers 가드 / httpx 클라이언트 close ---


class _HedraBodyCapture:
    """post 호출의 json body를 기록하는 httpx.Client 대역(폴링은 즉시 complete)."""

    def __init__(self):
        self.bodies: list[dict] = []

    def post(self, *args, **kwargs):
        self.bodies.append(kwargs.get("json") or {})
        return _HedraResp(json_data={"id": "gen-1"})

    def get(self, *args, **kwargs):
        return _HedraResp(json_data={"status": "complete", "url": "https://h/a.mp4"})


def test_hedra_body_uses_character_id_not_start_keyframe_id():
    """고정 마스코트는 Hedra의 character_id 필드로 보내야 한다(start_keyframe_id 아님)."""
    from nutti.integrations.video import HedraClient

    http = _HedraBodyCapture()
    client = HedraClient(_live_settings(), http=http, sleep=_no_sleep)
    client.render_character(text_prompt="대본", character_id="char-uuid-1")
    body = http.bodies[0]
    assert body.get("character_id") == "char-uuid-1"
    assert "start_keyframe_id" not in body


def test_kling_headers_guard_does_not_leak_key_without_http():
    """주입 http가 없으면 _headers()는 키를 구성하지 않고 NotImplementedError로 막는다."""
    client = KlingClient(_live_settings(kling_api_key="sk_kling_secret_123"))
    with pytest.raises(NotImplementedError):
        client._headers()


def test_kling_headers_ok_when_http_injected():
    """미리 서명된 토큰용 http가 주입되면 _headers()는 정상 동작한다."""
    settings = _live_settings()
    object.__setattr__(settings, "kling_api_key", "kl")  # .env 의존 제거(결정성).
    client = KlingClient(settings, http=_ScenesFakeHttp())
    headers = client._headers()
    assert headers["Authorization"] == "Bearer kl"


class _ClosableHttp:
    """close 호출 여부를 기록하는 httpx.Client 대역."""

    def __init__(self):
        self.closed = False

    def request(self, *args, **kwargs):
        return _ScenesFakeResponse(json_data={"id": "x", "status": "succeeded",
                                              "content": {"video_url": "https://c/x.mp4"}})

    def close(self):
        self.closed = True


def test_seedance_client_close_closes_http_idempotently():
    """SeedanceClient.close()는 주입된 httpx 클라이언트를 닫고 멱등하다."""
    http = _ClosableHttp()
    client = SeedanceClient(_live_settings(), http=http)
    client.close()
    assert http.closed is True
    client.close()  # 두 번째 호출도 예외 없이 통과(멱등).


def test_client_close_context_manager():
    """믹스인 컨텍스트 매니저가 블록 종료 시 http를 닫는다."""
    http = _ClosableHttp()
    with AssemblyAIClient("k", http=http):
        pass
    assert http.closed is True


def test_render_scenes_closes_self_created_client(monkeypatch):
    """주입이 없을 때 _render_scenes가 만든 씬 클라이언트의 http는 종료 후 닫힌다."""
    closed: list[bool] = []

    class _SelfClient:
        def submit(self, prompt):
            return "job-1"

        def poll(self, job_id):
            return "https://fake.local/s.mp4"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "nutti.integrations.video.VideoStudio._build_scenes_client",
        lambda self: _SelfClient(),
    )
    studio = VideoStudio(_live_settings(), sleep=_b_no_sleep)
    studio._render_scenes(_script())
    assert closed == [True]


def test_render_character_closes_self_created_hedra_client(monkeypatch):
    """주입이 없을 때 _render_character가 만든 HedraClient는 finally에서 닫힌다.

    `_render_scenes`의 close 핀과 동일한 패턴 — `nutti.integrations.video.HedraClient`를
    close() 호출을 기록하는 fake로 교체하고, live settings + 클라이언트 미주입으로
    `_render_character`를 호출한 뒤 close가 정확히 1회 불렸는지 단언한다.
    finally 블록 삭제 시 이 테스트가 빨간색이 돼 회귀를 즉시 감지한다."""
    closed: list[bool] = []

    class _FakeHedraWithClose:
        """render_character 인터페이스를 구현하고 close 호출을 기록하는 fake."""

        def render_character(self, *, text_prompt: str, character_id: str = "") -> str:
            return "https://fake.local/hedra/char.mp4"

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        "nutti.integrations.video.HedraClient",
        lambda settings, **kwargs: _FakeHedraWithClose(),
    )
    studio = VideoStudio(_live_settings(), sleep=_no_sleep)  # hedra_client 미주입
    url = studio._render_character(_script())
    assert url == "https://fake.local/hedra/char.mp4"
    # finally 블록이 살아 있으면 close가 정확히 1회 호출돼야 한다.
    assert closed == [True]
