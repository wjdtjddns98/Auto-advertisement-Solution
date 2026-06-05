"""영상 생성 연동: Hedra(캐릭터) · Seedance/Kling(씬) · AssemblyAI(자막).

실제 API 연동부는 TODO로 표시. dry_run에서는 더미 URL을 채워 파이프라인을 검증한다.
각 메서드의 시그니처/반환 형태는 실제 연동 시 그대로 유지하도록 설계했다.
"""

from __future__ import annotations

import time
from typing import Protocol

from nutti.config import Settings
from nutti.logging import get_logger
from nutti.models import Script, VideoAsset

log = get_logger(__name__)


def _usable_key(value: str | None) -> bool:
    """API 키 값이 실제로 쓸 수 있는지(비어 있지 않고 주석이 아님) 판정한다.

    pydantic-settings는 `.env`의 인라인 주석을 분리하지 않으므로,
    `KLEY=   # 설명`처럼 빈 값 뒤에 주석이 붙으면 키 값이 `'# 설명'`이라는
    truthy 문자열로 파싱된다. 단순 truthiness 검사는 이런 더미 값을 진짜 키로
    오인해 fast-fail 가드를 우회시키므로, strip 후 주석(`#` 시작)을 배제한다.
    """
    if not value:
        return False
    stripped = value.strip()
    return bool(stripped) and not stripped.startswith("#")


def _close_http(http) -> None:
    """httpx 호환 클라이언트를 안전하게 닫는다(close가 있으면 호출).

    주입된 fake에는 close가 없을 수 있으므로 getattr로 방어한다.
    """
    if http is not None:
        close = getattr(http, "close", None)
        if callable(close):
            close()


def _close_owned(client) -> None:
    """VideoStudio가 자체 생성한 연동 클라이언트를 안전하게 닫는다.

    close()를 가진 실 클라이언트는 그걸 호출하고, close가 없는 대체 구현
    (테스트 monkeypatch가 반환하는 fake 등)은 조용히 건너뛴다.
    """
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        close()


class _HttpClosingMixin:
    """지연 생성한 `httpx.Client`(self._http)를 닫는 close/컨텍스트 매니저 제공.

    각 영상 클라이언트는 self._http에 httpx.Client를 지연 캐싱하는데, 닫지 않으면
    장기 실행 스케줄러에서 TCP 연결 풀/파일 디스크립터가 누적된다. 이 믹스인은
    `close()`(멱등)와 `with` 지원을 더해 누수를 막는다. 주입받은 클라이언트도
    소유권이 호출부로 넘어온 것으로 보고 닫는다(호출부는 자체 생성분만 닫음).
    """

    _http = None

    def close(self) -> None:
        _close_http(self._http)
        self._http = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class VideoRenderError(RuntimeError):
    """영상 렌더(캐릭터/씬/합성) 실패. HTTP 4xx·5xx 등 영구 오류에 사용한다."""


class VideoTimeoutError(VideoRenderError):
    """렌더 작업이 폴링 제한 시간 안에 완료되지 않은 경우의 타임아웃."""


class SubtitleError(RuntimeError):
    """자막(AssemblyAI 전사) 생성 실패. 전사 상태가 'error'인 경우 등에 쓴다."""


# Hedra Public API 기본값(연구 노트 기준). Settings에 대응 필드가 생기면 그쪽을
# 우선 사용하고, 없으면 아래 기본값으로 폴백한다(config.py 변경 없이 동작).
_HEDRA_BASE = "https://api.hedra.com/web-app/public"
_HEDRA_POLL_INTERVAL_SEC = 5.0
_HEDRA_TIMEOUT_SEC = 300.0
# 폴링을 끝내는 종료 상태. 그 외(queued/processing/finalizing)는 계속 대기한다.
_HEDRA_DONE = "complete"
_HEDRA_ERROR = "error"


class HedraClient(_HttpClosingMixin):
    """Hedra Character-3 Public API 클라이언트(httpx 기반).

    실 경로(non-dry_run)에서만 생성되며, `httpx`는 메서드 안에서 lazy import한다
    (dry_run 환경에 네트워크 의존성을 강제하지 않기 위함). 인증은 `X-API-Key`
    헤더 방식이다(Bearer 아님). 생성은 `POST /generations`, 폴링은
    `GET /generations/{id}/status`를 사용한다.

    HTTP 4xx·5xx 또는 상태 'error'는 `VideoRenderError`로, 제한 시간 초과는
    `VideoTimeoutError`로 전파한다. 테스트는 `http`(httpx 호환 클라이언트)와
    `sleep`(가짜 시계)을 주입해 네트워크 없이 폴링·타임아웃·에러 경로를 검증한다.
    """

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        # 폴링 대기. 기본 time.sleep, 테스트에서 가짜 시계로 대체.
        self._sleep = sleep if sleep is not None else time.sleep
        # Settings에 폴링 설정 필드가 있으면 사용, 없으면 기본값(config.py 불변 보장).
        self._interval = float(
            getattr(settings, "hedra_poll_interval_sec", _HEDRA_POLL_INTERVAL_SEC)
        )
        self._timeout = float(getattr(settings, "hedra_timeout_sec", _HEDRA_TIMEOUT_SEC))
        self._model_id = getattr(settings, "hedra_model_id", "")

    def _client(self):
        """httpx 클라이언트를 지연 확보(주입 우선). dry_run에선 호출되지 않는다."""
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=30.0)
        return self._http

    @property
    def _headers(self) -> dict:
        # 인증: X-API-Key 헤더(Bearer 아님). 키 값은 settings에서 가져온다.
        return {
            "X-API-Key": self.settings.hedra_api_key,
            "Content-Type": "application/json",
        }

    def render_character(self, *, text_prompt: str, character_id: str = "") -> str:
        """립싱크 영상 생성을 요청하고 완료까지 폴링한 뒤 결과 URL을 반환한다."""
        generation_id = self._create_generation(text_prompt, character_id)
        return self._poll(generation_id)

    def _create_generation(self, text_prompt: str, character_id: str) -> str:
        """`POST /generations` — 생성 요청 후 generation id를 추출한다."""
        # TODO(live): 실제 Hedra Character-3 Public API 문서로 요청 바디 필드명을
        # 검증 필요 — generated_video_inputs/text_prompt/character_id 등은 연구 노트
        # 기반 추정이며, 키 확보 후 실제 응답으로 확정한다(필드명 불일치 시 400).
        body: dict = {
            "type": "video",
            "generated_video_inputs": {
                "text_prompt": text_prompt,
                "resolution": "720p",
                "aspect_ratio": "9:16",
            },
        }
        if self._model_id:
            body["ai_model_id"] = self._model_id
        if character_id:
            # 고정 마스코트(캐릭터) asset UUID. Hedra Public API에서 캐릭터 선택은
            # `character_id` 필드다(`start_keyframe_id`는 첫 프레임을 고정하는 별개의
            # 이미지 asset이므로, 캐릭터 UUID를 거기 넣으면 마스코트 고정이 깨진다).
            body["character_id"] = character_id

        resp = self._client().post(
            f"{_HEDRA_BASE}/generations", json=body, headers=self._headers
        )
        self._raise_for_status(resp, "Hedra 생성 요청")
        data = resp.json()
        generation_id = data.get("id")
        if not generation_id:
            # 원본 응답 본문에는 서명된 CDN URL·내부 토큰이 포함될 수 있으므로,
            # 전체 dict 대신 키 목록만 메시지에 포함한다(보안 redaction).
            # 디버깅이 필요하면 DEBUG 로그를 확인할 것.
            log.debug("hedra.create.missing_id", keys=list(data.keys()))
            raise VideoRenderError(
                f"Hedra 응답에 생성 id가 없습니다 (응답 키: {list(data.keys())})"
            )
        return str(generation_id)

    def _poll(self, generation_id: str) -> str:
        """`GET /generations/{id}/status`를 interval 간격으로 timeout까지 폴링."""
        # 경과 시간을 sleep 누적으로 추적(가짜 시계 주입 시에도 결정적).
        # 경계는 `< timeout`로 둔다 — `<=`면 deadline에 도달한 뒤에도 1회 더
        # 폴링해(off-by-one) 제한 시간을 초과한 호출이 발생한다.
        elapsed = 0.0
        while elapsed < self._timeout:
            resp = self._client().get(
                f"{_HEDRA_BASE}/generations/{generation_id}/status",
                headers=self._headers,
            )
            self._raise_for_status(resp, "Hedra 상태 조회")
            data = resp.json()
            status = data.get("status")
            if status == _HEDRA_DONE:
                # download_url 우선, 없으면 url로 폴백.
                url = data.get("download_url") or data.get("url")
                if not url:
                    # 원본 본문에는 서명된 CDN URL·내부 메타데이터가 있을 수 있어
                    # 전체 dict 대신 키 목록만 노출한다(보안 redaction).
                    log.debug("hedra.poll.missing_url", keys=list(data.keys()))
                    raise VideoRenderError(
                        f"Hedra 완료 응답에 URL이 없습니다 (응답 키: {list(data.keys())})"
                    )
                return str(url)
            if status == _HEDRA_ERROR:
                msg = data.get("error_message") or "원인 미상"
                raise VideoRenderError(f"Hedra 생성 실패: {msg}")
            # 아직 진행 중(queued/processing/finalizing) → 대기 후 재시도.
            self._sleep(self._interval)
            elapsed += self._interval
        raise VideoTimeoutError(f"Hedra 폴링 타임아웃({self._timeout:.0f}s): {generation_id}")

    @staticmethod
    def _raise_for_status(resp, what: str) -> None:
        """HTTP 4xx·5xx를 VideoRenderError로 전파(httpx의 status_code 사용).

        status_code 속성이 없는 응답은 200으로 가정하면 잘못된 fake/응답을 조용히
        통과시켜 무음 결함을 만든다. 따라서 status_code가 없거나 int가 아니면
        명시적으로 VideoRenderError를 던져 분명히 실패시킨다(방어적 파싱).
        """
        code = getattr(resp, "status_code", None)
        if not isinstance(code, int):
            raise VideoRenderError(f"{what} 응답에 유효한 status_code가 없습니다: {resp!r}")
        if code >= 400:
            raise VideoRenderError(f"{what} HTTP {code}")


class VideoStudio:
    """대본 → 최종 영상 합성을 담당하는 파사드(facade)."""

    def __init__(
        self,
        settings: Settings,
        *,
        hedra_client=None,
        scenes_client=None,
        assemblyai_client=None,
        composer_client=None,
        sleep=None,
    ):
        # 실연동 클라이언트는 주입 가능하게 받는다(테스트에서 fake 주입 → 네트워크 불요).
        # 주입이 없으면 각 실 경로(non-dry_run)에서 지연 생성한다.
        self.settings = settings
        self._hedra_client = hedra_client
        self._scenes_client = scenes_client
        self._assemblyai_client = assemblyai_client
        self._composer_client = composer_client
        # 폴링 대기용 sleep 주입(기본 time.sleep). 테스트에서 가짜 시계로 대체.
        self._sleep = sleep

    def validate_config(self) -> None:
        """실 경로 진입 전 필수 API 키가 비어 있지 않은지 한 번에 점검한다.

        dry_run이면 키가 없어도 되므로 즉시 통과한다. 실 경로(dry_run=False)에서
        키가 비어 있으면, 인증 401을 받고 나서야 불투명한 'HTTP 401'로 실패하는
        대신 시작 시점에 명확한 설정 오류(ValueError)로 빠르게 실패한다.
        주입된 클라이언트가 있으면 해당 키 검사는 건너뛴다(테스트/대체 구현 허용).
        """
        if self.settings.dry_run:
            return

        if self._hedra_client is None and not _usable_key(self.settings.hedra_api_key):
            raise ValueError("HEDRA_API_KEY가 비어 있습니다 — dry_run=False 시 필수입니다.")
        if self._scenes_client is None:
            # 씬 클라이언트는 Kling 키가 있으면 Kling, 없으면 Seedance를 쓴다.
            # 키 값은 _usable_key로 판정한다 — .env에 `KLING_API_KEY=  # 설명`처럼
            # 빈 값 뒤 인라인 주석이 있으면 pydantic-settings가 주석 문자열을 값으로
            # 파싱해 truthy가 되므로, 단순 truthiness로는 가드가 우회된다.
            if _usable_key(self.settings.kling_api_key):
                # Kling 키는 존재하나 JWT 서명이 미구현이라 KlingClient.http는 항상
                # NotImplementedError로 막힌다. 그 실패를 렌더 도중(잘못된 예외 타입)으로
                # 미루지 않고, 시작 시점에 ValueError로 빠르게 실패시킨다(fast-fail 계약).
                raise ValueError(
                    "KLING_API_KEY는 access_key_id/secret 기반 JWT 서명이 미구현이라 "
                    "직접 사용할 수 없습니다 — SEEDANCE_API_KEY를 쓰거나, 미리 서명된 "
                    "토큰을 쓰는 클라이언트를 scenes_client=로 주입하세요."
                )
            if not _usable_key(self.settings.seedance_api_key):
                raise ValueError(
                    "SEEDANCE_API_KEY가 비어 있습니다 — dry_run=False 시 필수입니다 "
                    "(KLING_API_KEY도 비어 있음)."
                )
        if self._assemblyai_client is None and not _usable_key(self.settings.assemblyai_api_key):
            raise ValueError("ASSEMBLYAI_API_KEY가 비어 있습니다 — dry_run=False 시 필수입니다.")

    def produce(self, script: Script) -> VideoAsset:
        """캐릭터 영상 + 씬 영상 + 자막을 합성해 최종 영상을 만든다."""
        # 실 경로면 시작 전에 필수 키를 한 번에 검증(미설정 시 빠르게 실패).
        self.validate_config()
        character = self._render_character(script)
        scenes = self._render_scenes(script)
        subtitle = self._generate_subtitles(character)
        final, preview = self._compose(character, scenes, subtitle)
        return VideoAsset(
            script_id=script.id,
            character_clip_url=character,
            scene_clip_urls=scenes,
            subtitle_url=subtitle,
            final_url=final,
            preview_url=preview,
            duration_sec=60.0,
        )

    def _render_character(self, script: Script) -> str:
        """Hedra Character-3: 고정 마스코트가 대본을 읽는 립싱크 영상.

        dry_run이면 더미 URL을 즉시 반환한다. 실 경로는 Hedra Public API를
        호출하는 클라이언트(`HedraClient` 또는 주입된 fake)에 위임한다:
        생성 요청 → 상태 폴링(complete까지) → 결과 URL 반환.
        """
        if self.settings.dry_run:
            log.info("dry_run.hedra", script_id=script.id)
            return f"https://dryrun.local/hedra/{script.id}.mp4"

        # 주입된 클라이언트는 소유자가 닫고, 여기서 만든 것만 finally에서 닫는다
        # (httpx 연결 풀 누수 방지). 주입분은 owned=None으로 둬 close하지 않는다.
        client = self._hedra_client
        owned = None
        if client is None:
            client = owned = HedraClient(self.settings, sleep=self._sleep)
        try:
            url = client.render_character(
                text_prompt=script.body,
                character_id=self.settings.hedra_character_id,
            )
        finally:
            if owned is not None:
                _close_owned(owned)
        log.info("hedra.character.done", script_id=script.id)
        return url

    def _render_scenes(self, script: Script) -> list[str]:
        """Seedance 2.0 / Kling 3.0: 배경 씬 영상.

        dry_run이면 더미 URL 2개를 즉시 반환한다. 실 경로는 씬 프롬프트마다
        생성 작업을 제출하고(`submit`) 작업 ID를 순차로 폴링(`poll`)해 URL을 모은다.
        클라이언트 선택 규칙: `settings.kling_api_key`가 채워져 있으면 고화질
        Kling 3.0, 그렇지 않으면 기본 Seedance 2.0을 사용한다(주입된 fake가 우선).
        """
        if self.settings.dry_run:
            log.info("dry_run.seedance", script_id=script.id)
            return [f"https://dryrun.local/seedance/{script.id}_scene{i}.mp4" for i in range(2)]

        # 주입분은 소유자가 닫고, 여기서 만든 것만 finally에서 닫는다(풀 누수 방지).
        client = self._scenes_client
        owned = None
        if client is None:
            client = owned = self._build_scenes_client()
        prompts = self._scene_prompts(script)
        urls: list[str] = []
        try:
            # 씬별로 작업을 제출하고(병렬 폴링은 추후 async로 전환 가능) 순차 폴링한다.
            for index, prompt in enumerate(prompts):
                job_id = client.submit(prompt)
                url = self._poll_scene(client, job_id, index)
                urls.append(url)
        finally:
            # 자체 생성분만 닫는다. close가 없는 대체 구현도 안전하게 다룬다.
            if owned is not None:
                _close_owned(owned)
        log.info("scenes.done", script_id=script.id, count=len(urls))
        return urls

    def _build_scenes_client(self) -> ScenesClientProtocol:
        """주입이 없을 때 설정에 따라 실 씬 클라이언트를 지연 생성한다.

        Kling 키가 있으면 고화질 Kling 3.0을, 없으면 기본 Seedance 2.0을 만든다.
        키 판정은 _usable_key로 한다(인라인 주석이 값으로 파싱되는 .env 패턴 방어).
        """
        if _usable_key(self.settings.kling_api_key):
            return KlingClient(self.settings, sleep=self._sleep)
        return SeedanceClient(self.settings, sleep=self._sleep)

    def _scene_prompts(self, script: Script) -> list[str]:
        """대본에서 씬 프롬프트 목록을 만든다.

        현재 Script에는 별도 scenes 필드가 없으므로 본문을 도입부/마무리 두 씬으로
        나눠 dry_run(2개)과 동일한 개수를 유지한다. 본문이 비어 있으면 주제를 쓴다.
        """
        text = script.body.strip() or script.topic
        return [f"{text} (씬 1: 도입)", f"{text} (씬 2: 마무리)"]

    def _poll_scene(self, client: ScenesClientProtocol, job_id: str, index: int) -> str:
        """단일 씬 작업을 완료까지 폴링해 URL을 반환한다.

        간격은 settings.scene_poll_interval_sec, 한도는 settings.scene_timeout_sec.
        제한 시간 초과 시 VideoTimeoutError를 전파한다. HTTP/상태 오류는 클라이언트가
        VideoRenderError로 올려 보낸다.
        """
        interval = getattr(self.settings, "scene_poll_interval_sec", 5.0)
        timeout = getattr(self.settings, "scene_timeout_sec", 300.0)
        sleep = self._sleep or time.sleep
        # 경계는 `< timeout`(off-by-one 방지: deadline 도달 후 추가 폴링 금지).
        elapsed = 0.0
        while elapsed < timeout:
            url = client.poll(job_id)
            if url:
                return url
            sleep(interval)
            elapsed += interval
        raise VideoTimeoutError(
            f"씬 {index} 렌더 타임아웃(job={job_id}, {timeout}s 초과)"
        )

    def _generate_subtitles(self, video_url: str) -> str:
        """AssemblyAI: 영상 URL을 전사해 SRT 자막 data-URL을 반환한다.

        dry_run이면 더미 `.srt` URL을 반환한다. 실 경로는 전사 잡 제출 →
        상태 폴링(`completed`/`error`) → SRT 다운로드 순으로 진행하며,
        주입된 클라이언트가 없으면 `AssemblyAIClient`를 지연 생성한다.
        - 상태가 `error`면 즉시 `SubtitleError`,
        - 폴링 제한 시간을 넘기면 `VideoTimeoutError`를 던진다.
        """
        if self.settings.dry_run:
            return video_url.replace(".mp4", ".srt")

        # 주입분은 소유자가 닫고, 여기서 만든 것만 finally에서 닫는다(풀 누수 방지).
        client = self._assemblyai_client
        owned = None
        if client is None:
            client = owned = AssemblyAIClient(self.settings.assemblyai_api_key)
        try:
            transcript_id = client.submit(video_url)

            interval = getattr(self.settings, "subtitle_poll_interval_sec", 5.0)
            timeout = getattr(self.settings, "subtitle_timeout_sec", 600.0)
            sleep = self._sleep or time.sleep
            # 경계는 `< timeout`(off-by-one 방지: deadline 도달 후 추가 폴링 금지).
            elapsed = 0.0
            while elapsed < timeout:
                status = client.poll(transcript_id)
                if status == "completed":
                    srt_text = client.fetch_srt(transcript_id)
                    log.info("assemblyai.subtitle.done", transcript_id=transcript_id)
                    return _as_srt_data_url(srt_text)
                if status == "error":
                    raise SubtitleError(f"AssemblyAI 전사 실패: transcript_id={transcript_id}")
                sleep(interval)
                elapsed += interval
            raise VideoTimeoutError(
                f"AssemblyAI 폴링 타임아웃(transcript={transcript_id}, {timeout}s 초과)"
            )
        finally:
            if owned is not None:
                _close_owned(owned)

    def _compose(self, character: str, scenes: list[str], subtitle: str) -> tuple[str, str]:
        """클립 합성 → (최종 URL, 미리보기 URL). 실제로는 ffmpeg/렌더 서비스.

        dry_run이면 더미 URL을 반환한다. 실 경로는 주입된 합성기에 위임하고,
        주입이 없으면 ffmpeg 기반 `BaseComposer`를 사용한다(ffmpeg 미설치 등
        실패는 `VideoRenderError`로 전파).
        """
        if self.settings.dry_run:
            final = character.replace("hedra", "final")
            return final, final.replace(".mp4", "_preview.gif")

        composer = self._composer_client or BaseComposer()
        return composer.compose(character, scenes, subtitle)


# --- WS-C: AssemblyAI 자막 + 합성 추상화 ---


def _as_srt_data_url(srt_text: str) -> str:
    """SRT 문자열을 자급 가능한 data-URL로 감싼다.

    스토리지 업로드 인프라가 붙기 전까지, 외부 호스팅 없이도 자막 내용을
    그대로 들고 다닐 수 있도록 base64 data-URL 형태로 반환한다.
    """
    import base64

    encoded = base64.b64encode(srt_text.encode("utf-8")).decode("ascii")
    return f"data:application/x-subrip;base64,{encoded}"


def _redact_http_error(exc: Exception) -> str:
    """HTTP/전송 예외를 사용자에게 보여줄 안전한 문자열로 정리한다.

    httpx의 예외 문자열에는 전체 요청 URL(transcript_id 등 식별자 포함)이 박혀
    있어 로그/에러 메시지로 새면 정보 노출이 된다. 따라서 HTTPStatusError는
    상태 코드만, 그 외 전송 오류는 예외 타입명만 남기고 URL/본문은 버린다.
    """
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        # 응답이 있으면 상태 코드만 노출(URL/본문 제외).
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", "?")
        return f"HTTP {code}"
    # 전송 오류(타임아웃·연결 실패 등)는 타입명만 — 메시지에 URL이 박혀 있을 수 있음.
    return type(exc).__name__


class AssemblyAIClient(_HttpClosingMixin):
    """AssemblyAI 전사 REST API(v2) 얇은 래퍼.

    영상/오디오 URL을 전사 잡으로 제출하고, 상태를 폴링한 뒤 SRT 자막을
    내려받는다. 인증 헤더는 Bearer 접두사 없이 키 값만 넣는다. httpx 클라이언트를
    주입하면 네트워크 없이 테스트할 수 있고, 주입이 없으면 지연 생성한다.

    HTTP/전송 오류는 VideoRenderError로 승격하되, 메시지에는 `_redact_http_error`로
    상태 코드/예외 타입만 남기고 요청 URL(식별자 포함)은 노출하지 않는다.
    """

    BASE_URL = "https://api.assemblyai.com"

    def __init__(self, api_key: str, *, http=None):
        self.api_key = api_key
        self._http = http

    @property
    def http(self):
        """주입된 httpx.Client가 없으면 지연 생성한다(실 경로에서만 httpx import)."""
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=httpx.Timeout(30.0))
        return self._http

    def _headers(self) -> dict[str, str]:
        # AssemblyAI는 Bearer 없이 키 값만 Authorization 헤더에 넣는다.
        return {"Authorization": self.api_key}

    def submit(self, audio_url: str) -> str:
        """전사 잡을 제출하고 transcript_id를 반환한다."""
        try:
            resp = self.http.post(
                f"{self.BASE_URL}/v2/transcript",
                headers=self._headers(),
                json={"audio_url": audio_url},
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - HTTP/전송 오류를 영구 실패로 승격
            # 메시지에 요청 URL이 새지 않도록 상태 코드/타입만 남긴다(redact).
            raise VideoRenderError(
                f"AssemblyAI 전사 잡 제출 실패: {_redact_http_error(exc)}"
            ) from None
        transcript_id = resp.json().get("id")
        if not transcript_id:
            raise VideoRenderError("AssemblyAI 응답에 transcript id가 없음")
        return str(transcript_id)

    def poll(self, transcript_id: str) -> str:
        """전사 상태 문자열을 반환한다(queued/processing/completed/error)."""
        try:
            resp = self.http.get(
                f"{self.BASE_URL}/v2/transcript/{transcript_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise VideoRenderError(
                f"AssemblyAI 상태 조회 실패: {_redact_http_error(exc)}"
            ) from None
        return str(resp.json().get("status", ""))

    def fetch_srt(self, transcript_id: str) -> str:
        """완료된 전사의 SRT 자막을 plain-text로 내려받는다."""
        try:
            resp = self.http.get(
                f"{self.BASE_URL}/v2/transcript/{transcript_id}/srt",
                headers=self._headers(),
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise VideoRenderError(
                f"AssemblyAI SRT 다운로드 실패: {_redact_http_error(exc)}"
            ) from None
        return resp.text


class ComposerProtocol(Protocol):
    """클립 합성기 인터페이스(주입 타입 힌트).

    캐릭터 영상·씬 영상 목록·자막을 받아 (최종 URL, 미리보기 URL)을 만든다.
    """

    def compose(
        self, character_url: str, scene_urls: list[str], subtitle_url: str
    ) -> tuple[str, str]:
        ...


class BaseComposer:
    """ffmpeg 기반 기본 합성기(스토리지 연동 전까지 미구현).

    실제 환경에서는 캐릭터+씬+자막을 ffmpeg로 합성(다운로드→concat→자막
    burn-in)한 뒤 스토리지에 업로드해 최종 URL을 확정해야 한다. 그러나 그
    파이프라인은 스토리지 연동과 함께 채워질 예정이라 아직 구현되지 않았다.

    과거에는 `character_url.replace("hedra", "final")` 같은 문자열 가공으로
    가짜 URL을 만들어 반환했는데, 실제 Hedra 응답 URL에는 'hedra' 부분 문자열이
    없거나(예: ``https://h/a.mp4``) 도메인에 박혀 있어(예:
    ``https://api.hedra.com/...``) 손상된 URL을 조용히 돌려주는 결함이 있었다.
    따라서 합성 결과를 확정할 수 없는 현 단계에서는 명시적으로
    `NotImplementedError`를 던져 무음 손상 대신 분명히 실패하게 한다.

    실 경로에서 합성이 필요하면 `composer_client=`로 실제 합성기를 주입하라.
    테스트에서는 FakeComposer를 주입해 이 경로를 타지 않는다.
    """

    def compose(
        self, character_url: str, scene_urls: list[str], subtitle_url: str
    ) -> tuple[str, str]:
        # 스토리지 업로드 인프라가 붙기 전까지 최종 URL을 확정할 수 없으므로,
        # 손상된 URL을 조용히 반환하는 대신 명시적으로 실패시킨다.
        raise NotImplementedError(
            "스토리지 연동 전까지 BaseComposer는 최종 영상 URL을 확정할 수 없습니다 "
            "— 실 경로에서는 composer_client=로 실제 합성기를 주입하세요."
        )


# --- WS-B: 배경 씬 생성 클라이언트(Seedance/Kling) ---


class ScenesClientProtocol(Protocol):
    """배경 씬 생성 클라이언트(Seedance/Kling)의 공통 인터페이스.

    fake 주입 시 타입 힌트를 명확히 하기 위한 구조적(structural) 프로토콜이다.
    구현체는 프롬프트마다 생성 작업을 제출(`submit`)하고, 작업 ID를 폴링(`poll`)해
    완료 시 영상 URL을, 진행 중이면 None을 반환한다. HTTP·상태 영구 오류는
    VideoRenderError로 전파한다.
    """

    def submit(self, prompt: str) -> str:
        """씬 생성 작업을 제출하고 작업 ID를 반환한다."""
        ...

    def poll(self, job_id: str) -> str | None:
        """작업 상태를 조회한다. 완료면 영상 URL, 진행 중이면 None을 반환한다."""
        ...


class SeedanceClient(_HttpClosingMixin):
    """Seedance 2.0(BytePlus/Volcengine ModelArk) REST 클라이언트(기본·표준 화질).

    인증은 `Authorization: Bearer <ARK_API_KEY>`. 씬마다 작업 1건을 만들고
    (`POST .../tasks`), 작업 ID로 상태를 폴링한다(`GET .../tasks/{id}`).
    httpx는 실 경로에서만 lazy import한다.
    """

    BASE = "https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks"
    MODEL = "dreamina-seedance-2-0-260128"
    # 실패로 간주하는 종료 상태 열거값.
    _TERMINAL_FAIL = {"failed", "expired", "cancelled"}

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http  # 테스트에서 httpx.Client 호환 fake 주입 가능.
        self._sleep = sleep

    @property
    def http(self):
        """httpx.Client를 지연 생성한다(주입이 없을 때만, 실 경로 전용)."""
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=httpx.Timeout(30.0))
        return self._http

    def _headers(self) -> dict:
        # ARK는 독립 헤더가 없고 Bearer 토큰으로 키를 전달한다(seedance_api_key 사용).
        return {
            "Authorization": f"Bearer {self.settings.seedance_api_key}",
            "Content-Type": "application/json",
        }

    def submit(self, prompt: str) -> str:
        """씬 생성 작업을 제출하고 작업 ID(`id`)를 반환한다."""
        body = {
            "model": self.MODEL,
            "content": [{"type": "text", "text": prompt}],
            "ratio": "9:16",
            "resolution": "720p",
            "duration": 5,
            "generate_audio": False,
            "watermark": False,
        }
        data = _request_json(self.http, "POST", self.BASE, headers=self._headers(), json=body)
        job_id = data.get("id")
        if not job_id:
            # 원본 본문에는 내부 job id·메타데이터가 포함될 수 있어 키 목록만 노출.
            log.debug("seedance.submit.missing_id", keys=list(data.keys()))
            raise VideoRenderError(
                f"Seedance 작업 ID 없음 (응답 키: {list(data.keys())})"
            )
        return str(job_id)

    def poll(self, job_id: str) -> str | None:
        """작업 상태를 조회한다. succeeded면 video_url, 진행 중이면 None."""
        data = _request_json(
            self.http, "GET", f"{self.BASE}/{job_id}", headers=self._headers()
        )
        status = data.get("status")
        if status == "succeeded":
            url = (data.get("content") or {}).get("video_url")
            if not url:
                # 원본 본문 전체 대신 키 목록만 — 서명된 URL·내부 메타데이터 노출 방지.
                log.debug("seedance.poll.missing_url", keys=list(data.keys()))
                raise VideoRenderError(
                    f"Seedance 완료했으나 video_url 없음 (응답 키: {list(data.keys())})"
                )
            return str(url)
        if status in self._TERMINAL_FAIL:
            # 오류 메시지는 status 문자열과 error 코드/타입만 — 원본 dict 금지.
            error_info = data.get("error")
            error_code = (
                error_info.get("code") if isinstance(error_info, dict) else str(error_info)
            )
            raise VideoRenderError(f"Seedance 작업 실패: status={status}, error_code={error_code}")
        return None  # queued/running 등 진행 중.


class KlingClient(_HttpClosingMixin):
    """Kling 3.0 REST 클라이언트(고화질 옵션, kling_api_key가 있을 때 사용).

    공식 Kling API(api.klingai.com)는 access_key_id + access_key_secret 한 쌍에서
    HMAC-SHA256으로 서명한 JWT 토큰을 요구한다. 그러나 `settings.kling_api_key`는
    단일 문자열이라 JWT를 생성할 두 자격증명이 없으므로, 이 키를 그대로 Bearer로
    보내면 운영에서 항상 401로 실패한다.

    무음 401을 막기 위해, 직접(주입 없이) 실 HTTP를 시도하면 명시적으로
    `NotImplementedError`를 던진다. JWT 서명이 구현되거나, 외부에서 미리 서명된
    토큰을 쓰는 httpx 클라이언트를 `http=`로 주입한 경우에만 실 호출을 허용한다.
    (테스트는 fake http를 주입하므로 이 가드를 통과한다.)

    씬마다 text2video 작업을 만들고 폴링한다.
    """

    SUBMIT_URL = "https://api.klingai.com/v1/videos/text2video"
    POLL_URL = "https://api.klingai.com/v1/videos/text2video"
    MODEL = "kling-v3-0"

    def __init__(self, settings: Settings, *, http=None, sleep=None):
        self.settings = settings
        self._http = http
        self._sleep = sleep

    @property
    def http(self):
        if self._http is None:
            # TODO(live): 실제 Kling 문서로 인증 방식 검증/구현 필요 — 공식 API는
            # access_key_id/secret에서 HMAC-SHA256으로 서명한 JWT를 요구한다(단일
            # kling_api_key로는 불가). JWT 서명 구현 전까지 live Kling 경로는 미기능.
            # JWT 서명 미구현 상태에서 raw 키를 Bearer로 보내면 401로 무음 실패하므로,
            # 미리 서명된 토큰을 쓰는 클라이언트가 주입되지 않았다면 분명히 실패시킨다.
            raise NotImplementedError(
                "Kling 직접 호출은 access_key_id/secret 기반 JWT 서명이 필요한데 "
                "아직 구현되지 않았습니다 — KLING_API_KEY를 Bearer로 보내면 401로 "
                "실패합니다. 미리 서명된 토큰을 쓰는 http= 클라이언트를 주입하거나 "
                "기본 Seedance 경로를 사용하세요."
            )
        return self._http

    def _headers(self) -> dict:
        # http 프로퍼티와 동일한 가드: JWT 미구현 상태에서 raw 키를 Bearer로
        # 구성하지 않는다. 주입 클라이언트가 없으면 키를 메모리에 만들지 않고 막아,
        # 디버깅/리팩터로 _headers()가 단독 호출될 때 키가 노출되는 것을 방지한다.
        if self._http is None:
            raise NotImplementedError(
                "Kling JWT 서명이 미구현이라 _headers()를 구성할 수 없습니다 — "
                "미리 서명된 토큰을 쓰는 http= 클라이언트를 주입하세요."
            )
        return {
            "Authorization": f"Bearer {self.settings.kling_api_key}",
            "Content-Type": "application/json",
        }

    def submit(self, prompt: str) -> str:
        """씬 생성 작업을 제출하고 task_id를 반환한다(고화질 mode=pro)."""
        # TODO(live): 실제 Kling 문서로 요청 바디 필드명(model_name/mode/aspect_ratio
        # 등)과 응답 스키마(data.task_id, task_status='succeed') 검증 필요 — 키/JWT
        # 확보 후 확정. 현재는 연구 노트 기반 추정값이다.
        body = {
            "model_name": self.MODEL,
            "prompt": prompt,
            "mode": "pro",
            "aspect_ratio": "9:16",
            "duration": "5",
        }
        data = _request_json(
            self.http, "POST", self.SUBMIT_URL, headers=self._headers(), json=body
        )
        # Kling은 논리 오류 시 code != 0을 반환하므로 방어적으로 검사.
        if data.get("code", 0) != 0:
            # message는 API 수준 오류 설명이라 노출 허용; 전체 dict는 금지.
            raise VideoRenderError(f"Kling 제출 오류: {data.get('message')}")
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            # 원본 본문에는 내부 job id·서명 토큰이 있을 수 있어 키 목록만 노출.
            top_keys = list(data.keys())
            data_keys = list((data.get("data") or {}).keys())
            log.debug("kling.submit.missing_task_id", top_keys=top_keys, data_keys=data_keys)
            raise VideoRenderError(
                f"Kling task_id 없음 (응답 키: {top_keys}, data 키: {data_keys})"
            )
        return str(task_id)

    def poll(self, job_id: str) -> str | None:
        """작업 상태를 조회한다. succeed면 영상 URL, 진행 중이면 None."""
        data = _request_json(
            self.http, "GET", f"{self.POLL_URL}/{job_id}", headers=self._headers()
        )
        if data.get("code", 0) != 0:
            # message는 API 수준 오류 설명이라 노출 허용; 전체 dict는 금지.
            raise VideoRenderError(f"Kling 폴링 오류: {data.get('message')}")
        payload = data.get("data") or {}
        status = payload.get("task_status")
        if status == "succeed":
            videos = (payload.get("task_result") or {}).get("videos") or []
            if not videos or not videos[0].get("url"):
                # 원본 본문에는 서명된 CDN URL·내부 메타데이터가 있을 수 있어 키 목록만.
                top_keys = list(data.keys())
                payload_keys = list(payload.keys())
                log.debug("kling.poll.missing_url", top_keys=top_keys, payload_keys=payload_keys)
                raise VideoRenderError(
                    f"Kling 완료했으나 URL 없음 (응답 키: {top_keys}, data 키: {payload_keys})"
                )
            return str(videos[0]["url"])
        if status == "failed":
            # task_status_msg는 API 수준 메시지라 노출 허용; 전체 payload dict는 금지.
            raise VideoRenderError(f"Kling 작업 실패: {payload.get('task_status_msg')}")
        return None  # submitted/processing 등 진행 중.


def _request_json(http, method: str, url: str, *, headers: dict, json: dict | None = None) -> dict:
    """httpx 호환 클라이언트로 요청하고 JSON을 반환한다(HTTP 오류는 VideoRenderError).

    httpx는 호출부에서만 lazy import하므로 예외 타입을 여기서 지연 참조한다.
    상태 코드 오류(4xx/5xx)와 전송 계층 오류 모두 영구 렌더 오류로 승격한다.
    """
    import httpx

    try:
        resp = http.request(method, url, headers=headers, json=json)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # 상태 코드만 노출(전체 URL은 식별자가 박혀 있을 수 있어 제외).
        status = exc.response.status_code
        raise VideoRenderError(f"씬 렌더 HTTP {status}") from None
    except httpx.HTTPError as exc:
        # 전송 오류는 예외 타입명만(메시지에 URL이 박힐 수 있음).
        raise VideoRenderError(f"씬 렌더 통신 오류: {type(exc).__name__}") from None
    except Exception as exc:  # noqa: BLE001 - httpx 외 전송 오류(ConnectionError 등)도 승격
        # 비-httpx 어댑터/주입 클라이언트가 stdlib 예외(ConnectionError·OSError 등)를
        # 던질 수 있으므로, 오케스트레이터가 기대하는 VideoRenderError로 통일한다.
        # URL/본문 노출을 피하려 타입명만 남긴다.
        raise VideoRenderError(f"씬 렌더 통신 오류: {type(exc).__name__}") from None
    return resp.json()
