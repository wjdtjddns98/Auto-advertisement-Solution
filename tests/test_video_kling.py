"""Kling 보이스오버 백엔드 단위 테스트.

대상: KlingClient, GeminiTtsClient, KlingVoiceoverBackend,
     KlingPromptBuilder, 모듈 헬퍼 함수들.
VideoStudio의 kling 분기(video_backend="kling") 포함.

모든 테스트는 fake 클라이언트/sleep 주입으로 **네트워크 없이** 동작한다.
ffmpeg(_mux)는 monkeypatch로 우회한다.

섹션 구성:
  A. VideoStudio kling 분기 & 키 검증 게이트
  B. KlingClient — 제출·폴링·다운로드·오류·SSRF 방어
  C. SSRF/입력 검증 순수 함수
  D. GeminiTtsClient — 합성·오류·파싱
  E. 순수 헬퍼(_pick_clip_duration 등)
  F. KlingVoiceoverBackend.produce_beat_clips
"""

from __future__ import annotations

import base64
import io
import wave
from pathlib import Path

import pytest

import nutti.integrations.video_kling as kling_module
from nutti.config import Settings
from nutti.integrations.video import (
    VideoRenderError,
    VideoStudio,
    VideoTimeoutError,
)
from nutti.integrations.video_kling import (
    GeminiTtsClient,
    KlingClient,
    KlingVoiceoverBackend,
    _parse_pcm_rate,
    _pcm_to_wav_bytes,
    _pick_clip_duration,
    _validate_fal_video_url,
    _validate_model_id,
    _validate_request_id,
)
from nutti.models import Script


# ─────────────────────────── 공통 헬퍼 ───────────────────────────


def _dry_settings(**overrides) -> Settings:
    """dry_run 환경 설정(네트워크/키 불요)."""
    base: dict = {"NUTTI_DRY_RUN": True}
    base.update(overrides)
    return Settings(**base)


def _live_settings(**overrides) -> Settings:
    """실 경로(non-dry_run) 설정. 키는 기본 빈 값."""
    base: dict = {"NUTTI_DRY_RUN": False, "GEMINI_API_KEY": "", "FAL_KEY": ""}
    base.update(overrides)
    return Settings(**base)


def _kling_settings(**overrides) -> Settings:
    """kling 백엔드 설정(실 경로, 키 채워짐)."""
    base: dict = {
        "NUTTI_DRY_RUN": False,
        "GEMINI_API_KEY": "test-gemini-key",
        "FAL_KEY": "test-fal-key",
        "NUTTI_VIDEO_BACKEND": "kling",
        "NUTTI_KLING_POLL_INTERVAL_SEC": 1.0,
        "NUTTI_KLING_TIMEOUT_SEC": 30.0,
    }
    base.update(overrides)
    return Settings(**base)


def _no_sleep(_seconds):
    """폴링 대기 없이 즉시 반환하는 가짜 sleep."""
    return None


def _frame_file(tmp_path) -> str:
    """KlingClient._submit이 읽을 시작 프레임 파일을 만들어 경로 반환."""
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"FAKE-FRAME-BYTES")
    return str(frame)


def _script(
    topic: str = "강아지 간식",
    body: str = "누띠 간식은 건강해요!",
    beats: list[str] | None = None,
) -> Script:
    """테스트용 최소 대본."""
    return Script(topic=topic, body=body, beats=beats or [])


def _make_pcm(num_samples: int = 100, rate: int = 24000) -> bytes:
    """테스트용 단순 16-bit mono PCM 바이트(0으로 채운 더미 신호)."""
    return b"\x00\x00" * num_samples


# ─────────────────────────── Fake HTTP ───────────────────────────


class _Resp:
    """httpx.Response 대역(status_code + headers + json + content)."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data=None,
        content: bytes = b"",
        json_exc: Exception | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.content = content
        self._json_exc = json_exc
        self.headers = dict(headers or {})

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json


class FakeKlingHttp:
    """KlingClient 주입용 fake HTTP 클라이언트.

    라우팅:
    - POST  → 제출 응답(post_response)
    - GET   request_id를 포함하는 /status URL → 폴링 큐(get_status_responses)
    - GET   request_id를 포함하는 결과 URL (status 없음) → 결과 응답(get_result_response)
    - GET   fal.media URL → 다운로드 응답(download_response)

    헤더 기록: 각 요청 유형별 헤더를 기록해 자격증명 격리를 검증한다.
    """

    def __init__(
        self,
        *,
        post_response: _Resp | None = None,
        post_exc: Exception | None = None,
        get_status_responses: list | None = None,
        get_result_response: _Resp | None = None,
        download_response: _Resp | Exception | None = None,
    ):
        self.post_response = post_response or _Resp(json_data={"request_id": "req-test-001"})
        self.post_exc = post_exc
        self.get_status_responses = list(get_status_responses or [])
        self.get_result_response = get_result_response or _Resp(
            json_data={"video": {"url": "https://fal.media/fake/video.mp4"}}
        )
        self.download_response = (
            download_response
            if download_response is not None
            else _Resp(content=b"FAKE-MP4-BYTES")
        )
        self.post_calls: list[tuple[str, dict | None]] = []
        self.post_headers: list[dict | None] = []
        self.status_calls: list[str] = []
        self.status_headers: list[dict | None] = []
        self.result_calls: list[str] = []
        self.result_headers: list[dict | None] = []
        self.download_calls: list[str] = []
        self.download_headers: list[dict | None] = []
        self.closed = False

    def post(self, url, *, headers=None, json=None):
        self.post_calls.append((url, json))
        self.post_headers.append(headers)
        if self.post_exc is not None:
            raise self.post_exc
        return self.post_response

    def get(self, url, *, headers=None, follow_redirects=False):
        # 큐 호스트(queue.fal.run)는 상태·결과 URL — 다운로드와 구별한다.
        is_queue_host = "queue.fal.run" in url
        # 상태 URL: 큐 호스트 + /status suffix
        if is_queue_host and url.endswith("/status"):
            self.status_calls.append(url)
            self.status_headers.append(headers)
            item = self.get_status_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        # 결과 URL: 큐 호스트 + /status 아님 (requests/{id} 경로)
        if is_queue_host:
            self.result_calls.append(url)
            self.result_headers.append(headers)
            return self.get_result_response
        # 다운로드 URL: fal.media 또는 fal.run(큐 아님)
        self.download_calls.append(url)
        self.download_headers.append(headers)
        if isinstance(self.download_response, Exception):
            raise self.download_response
        return self.download_response

    def close(self):
        self.closed = True


class FakeTtsHttp:
    """GeminiTtsClient 주입용 fake HTTP 클라이언트."""

    def __init__(
        self,
        *,
        response: _Resp | None = None,
        exc: Exception | None = None,
    ):
        self.response = response
        self.exc = exc
        self.post_calls: list[tuple[str, dict | None]] = []
        self.post_headers: list[dict | None] = []
        self.closed = False

    def post(self, url, *, headers=None, json=None):
        self.post_calls.append((url, json))
        self.post_headers.append(headers)
        if self.exc is not None:
            raise self.exc
        return self.response

    def close(self):
        self.closed = True


def _tts_audio_response(pcm: bytes = b"\x00\x00" * 100, rate: int = 24000) -> _Resp:
    """GeminiTtsClient 성공 응답(inline_data 오디오 파트)."""
    encoded = base64.b64encode(pcm).decode("ascii")
    return _Resp(
        json_data={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inline_data": {
                                    "mime_type": f"audio/L16;rate={rate}",
                                    "data": encoded,
                                }
                            }
                        ]
                    }
                }
            ]
        }
    )


def _tts_audio_response_camelcase(pcm: bytes = b"\x00\x00" * 50) -> _Resp:
    """camelCase inlineData 키를 사용하는 TTS 응답(실 API 변종)."""
    encoded = base64.b64encode(pcm).decode("ascii")
    return _Resp(
        json_data={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/L16;rate=24000",
                                    "data": encoded,
                                }
                            }
                        ]
                    }
                }
            ]
        }
    )


def _kling_client(tmp_path, fake, **setting_overrides) -> KlingClient:
    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path), **setting_overrides)
    return KlingClient(settings, http=fake, sleep=_no_sleep)


def _tts_client(tmp_path, fake, **setting_overrides) -> GeminiTtsClient:
    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path), **setting_overrides)
    return GeminiTtsClient(settings, http=fake, sleep=_no_sleep)


# ═══════════════════════════════════════════════════════════════════
# 섹션 A. VideoStudio kling 분기 & 게이트
# ═══════════════════════════════════════════════════════════════════


class FakeKlingClient:
    """KlingClient 대역 — generate 호출 인자를 기록하고 결정적 경로를 반환한다."""

    def __init__(self, video_path: str = "data/fake/kling.mp4"):
        self.video_path = video_path
        self.calls: list[tuple[str, str, int]] = []
        self.close_count = 0

    def generate(self, frame_path: str, prompt: str, duration_sec: int) -> str:
        self.calls.append((frame_path, prompt, duration_sec))
        return self.video_path

    def close(self):
        self.close_count += 1


class FakeTtsClient:
    """GeminiTtsClient 대역 — synthesize 호출 인자를 기록하고 결정적 (경로, 초)를 반환한다."""

    def __init__(self, audio_path: str = "data/fake/voice.wav", duration: float = 4.0):
        self.audio_path = audio_path
        self.duration = duration
        self.calls: list[str] = []
        self.close_count = 0

    def synthesize(self, text: str) -> tuple[str, float]:
        self.calls.append(text)
        return self.audio_path, self.duration

    def close(self):
        self.close_count += 1


class FakeNanoBananaClient:
    """NanoBananaClient 대역 — 결정적 프레임 경로를 반환한다."""

    def __init__(self, frame_path: str = "data/fake/frame.jpg"):
        self.frame_path = frame_path
        self.calls: list[tuple[str, str | None]] = []
        self.close_count = 0

    def generate_frame(self, scene_prompt: str, *, reference_image_path: str | None = None) -> str:
        self.calls.append((scene_prompt, reference_image_path))
        return self.frame_path

    def close(self):
        self.close_count += 1


def test_videostudio_kling_backend_routes_to_kling_path(monkeypatch):
    """video_backend='kling'이면 _produce_clips가 KlingVoiceoverBackend 경로로 분기한다.

    주입한 fake kling_client / tts_client가 실제로 사용되는지 확인한다.
    """
    kling = FakeKlingClient(video_path="data/fake/kling.mp4")
    tts = FakeTtsClient(audio_path="data/fake/voice.wav", duration=4.0)
    nano = FakeNanoBananaClient(frame_path="data/fake/frame.jpg")

    # _mux와 _stitch를 monkeypatch로 우회
    monkeypatch.setattr(KlingVoiceoverBackend, "_mux", lambda self, v, a: "data/fake/beat.mp4")
    monkeypatch.setattr(VideoStudio, "_stitch", lambda self, clips: clips[0])

    settings = _kling_settings(NUTTI_VIDEO_BACKEND="kling")
    studio = VideoStudio(settings, nano_client=nano, kling_client=kling, tts_client=tts)
    script = _script(body="간식 소개", beats=["비트1"])
    asset = studio.produce(script)

    # kling_client.generate가 호출됐어야 한다
    assert len(kling.calls) == 1
    # tts_client.synthesize가 호출됐어야 한다
    assert len(tts.calls) == 1
    assert tts.calls[0] == "비트1"
    assert asset.script_id == script.id


def test_videostudio_kling_duration_uses_measured_audio_not_8xn(monkeypatch):
    """kling 백엔드 duration_sec은 8×N이 아니라 실측 audio 길이 합 기반이다(#4 회귀 가드).

    비트 3개 × audio_sec=4.0 → 각 클립 min(clip_dur=5, 4.0)=4.0 → 총 12.0초.
    veo의 8×3=24.0과 명확히 다르다 — 8×N로 되돌리면 이 단언이 실패한다.
    """
    kling = FakeKlingClient()
    tts = FakeTtsClient(duration=4.0)  # audio 4.0초 → clip 5초, mux -shortest → 4.0초
    nano = FakeNanoBananaClient()

    monkeypatch.setattr(KlingVoiceoverBackend, "_mux", lambda self, v, a: "data/fake/beat.mp4")
    monkeypatch.setattr(VideoStudio, "_stitch", lambda self, clips: clips[0])

    settings = _kling_settings(NUTTI_VIDEO_BACKEND="kling")
    studio = VideoStudio(settings, nano_client=nano, kling_client=kling, tts_client=tts)
    script = _script(beats=["비트1", "비트2", "비트3"])
    asset = studio.produce(script)

    assert asset.duration_sec == pytest.approx(12.0)
    assert asset.duration_sec != pytest.approx(24.0)  # 8×N 가정이 아님


def test_videostudio_kling_dry_run_no_external_call(monkeypatch):
    """dry_run=True이면 kling 백엔드가 아무 외부 호출 없이 결정적 더미 자산을 반환한다."""
    kling = FakeKlingClient()
    tts = FakeTtsClient()
    settings = _dry_settings(NUTTI_VIDEO_BACKEND="kling")
    studio = VideoStudio(settings, kling_client=kling, tts_client=tts)
    asset = studio.produce(_script())
    # dry_run에서는 fake 클라이언트가 호출되지 않는다
    assert len(kling.calls) == 0
    assert len(tts.calls) == 0
    assert asset.final_url is not None


def test_videostudio_kling_validate_config_missing_gemini_key_raises():
    """kling 백엔드 + dry_run=False + GEMINI_API_KEY 빈 값 → ValueError."""
    settings = _live_settings(NUTTI_VIDEO_BACKEND="kling", GEMINI_API_KEY="", FAL_KEY="fk")
    studio = VideoStudio(settings)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        studio.validate_config()


def test_videostudio_kling_validate_config_missing_fal_key_raises():
    """kling 백엔드 + dry_run=False + FAL_KEY 빈 값 → ValueError."""
    settings = _live_settings(NUTTI_VIDEO_BACKEND="kling", GEMINI_API_KEY="gk", FAL_KEY="")
    studio = VideoStudio(settings)
    with pytest.raises(ValueError, match="FAL_KEY"):
        studio.validate_config()


def test_videostudio_kling_validate_config_injected_tts_skips_gemini_key():
    """tts_client만 주입해도 nano_client가 없으면 프레임용 GEMINI_API_KEY 검증이 먼저 발화한다.

    tts_client 주입은 TTS용 GEMINI_API_KEY 검사만 건너뛰게 한다 — 시작 프레임은
    백엔드 무관하게 NanoBanana(Gemini)로 만들므로 nano_client 미주입 + 키 없음이면
    GEMINI_API_KEY 검증이 먼저 막는다(검증 순서·메시지를 match로 고정).
    """
    settings = _live_settings(NUTTI_VIDEO_BACKEND="kling", GEMINI_API_KEY="", FAL_KEY="")
    # tts_client만 주입(nano_client·kling_client 미주입) → 첫 GEMINI_API_KEY 검사가 발화.
    studio = VideoStudio(settings, tts_client=FakeTtsClient())
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        studio.validate_config()


def test_videostudio_kling_validate_config_all_injected_skips_key_check():
    """nano_client + tts_client + kling_client 모두 주입 → 키 검사 없이 통과."""
    settings = _live_settings(NUTTI_VIDEO_BACKEND="kling", GEMINI_API_KEY="", FAL_KEY="")
    studio = VideoStudio(
        settings,
        nano_client=FakeNanoBananaClient(),
        kling_client=FakeKlingClient(),
        tts_client=FakeTtsClient(),
    )
    # 예외 없이 통과해야 한다
    studio.validate_config()


def test_videostudio_kling_validate_config_tts_branch_isolated_raises():
    """두 번째 분기(tts_client=None + GEMINI 빈 값) 독립 핀: nano·kling 주입해 첫·셋째 분기를 건너뛴다.

    nano_client 주입 → 첫 GEMINI 검사(프레임) 통과, kling_client 주입 → FAL 검사 통과.
    tts_client만 미주입이라 video.py의 두 번째 if(TTS용 GEMINI_API_KEY)만 발화해야 한다.
    이 테스트가 없으면 해당 분기를 삭제해도 기존 테스트가 모두 통과한다(회귀 사각지대).
    """
    settings = _live_settings(NUTTI_VIDEO_BACKEND="kling", GEMINI_API_KEY="", FAL_KEY="")
    studio = VideoStudio(
        settings,
        nano_client=FakeNanoBananaClient(),
        kling_client=FakeKlingClient(),
        # tts_client 미주입 → 두 번째 분기만 발화
    )
    with pytest.raises(ValueError, match="kling 백엔드의 TTS에 필수"):
        studio.validate_config()


def test_produce_beat_clips_cleans_intermediates_on_success(tmp_path, monkeypatch):
    """성공 흐름: 각 비트의 무음 영상·내레이션 WAV 중간물은 mux 후 삭제되고 beat 클립만 남는다."""
    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))

    counter = {"n": 0}

    class _FileTts:
        def synthesize(self, beat):
            counter["n"] += 1
            p = tmp_path / f"voice_{counter['n']}.wav"
            p.write_bytes(b"RIFFvoice")
            return str(p), 4.0

    class _FileKling:
        def generate(self, frame, prompt, dur):
            p = tmp_path / f"kling_{counter['n']}.mp4"
            p.write_bytes(b"\x00silent")
            return str(p)

    def fake_mux(self, video_path, audio_path):
        out = tmp_path / f"beat_{Path(video_path).stem}.mp4"
        out.write_bytes(b"\x00muxed")
        return str(out)

    monkeypatch.setattr(KlingVoiceoverBackend, "_mux", fake_mux)
    backend = KlingVoiceoverBackend(settings, kling_client=_FileKling(), tts_client=_FileTts())

    clips, total = backend.produce_beat_clips("frame.png", ["b1", "b2"])

    assert len(clips) == 2
    assert all(Path(c).exists() for c in clips)  # 결과 클립은 보존
    assert list(tmp_path.glob("voice_*.wav")) == []  # 내레이션 중간물 정리
    assert list(tmp_path.glob("kling_*.mp4")) == []  # 무음 영상 중간물 정리


def test_produce_beat_clips_cleans_completed_clips_on_midloop_failure(tmp_path, monkeypatch):
    """중도 실패: 2번 비트 mux 실패 시 이미 만든 beat 클립·모든 중간물이 leak 없이 정리된다."""
    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))

    counter = {"n": 0}

    class _FileTts:
        def synthesize(self, beat):
            counter["n"] += 1
            p = tmp_path / f"voice_{counter['n']}.wav"
            p.write_bytes(b"RIFFvoice")
            return str(p), 4.0

    class _FileKling:
        def generate(self, frame, prompt, dur):
            p = tmp_path / f"kling_{counter['n']}.mp4"
            p.write_bytes(b"\x00silent")
            return str(p)

    mux_calls = {"n": 0}

    def fail_second_mux(self, video_path, audio_path):
        mux_calls["n"] += 1
        if mux_calls["n"] == 2:
            raise VideoRenderError("2번 비트 mux 강제 실패")
        out = tmp_path / f"beat_{mux_calls['n']}.mp4"
        out.write_bytes(b"\x00muxed")
        return str(out)

    monkeypatch.setattr(KlingVoiceoverBackend, "_mux", fail_second_mux)
    backend = KlingVoiceoverBackend(settings, kling_client=_FileKling(), tts_client=_FileTts())

    with pytest.raises(VideoRenderError):
        backend.produce_beat_clips("frame.png", ["b1", "b2", "b3"])

    # 1번 비트 완성 클립(beat_1.mp4)도 정리되고, 모든 중간물도 leak 없음
    assert list(tmp_path.glob("beat_*.mp4")) == []
    assert list(tmp_path.glob("voice_*.wav")) == []
    assert list(tmp_path.glob("kling_*.mp4")) == []


# ═══════════════════════════════════════════════════════════════════
# 섹션 B. KlingClient
# ═══════════════════════════════════════════════════════════════════


def test_kling_client_generate_success_returns_path(tmp_path):
    """정상 흐름: 제출 → IN_QUEUE → IN_PROGRESS → COMPLETED → 다운로드 → 경로 반환."""
    fake = FakeKlingHttp(
        post_response=_Resp(json_data={"request_id": "req-abc-001"}),
        get_status_responses=[
            _Resp(json_data={"status": "IN_QUEUE"}),
            _Resp(json_data={"status": "IN_PROGRESS"}),
            _Resp(json_data={"status": "COMPLETED"}),
        ],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/video123.mp4"}}
        ),
        download_response=_Resp(content=b"FAKE-KLING-MP4"),
    )
    client = _kling_client(tmp_path, fake)
    path = client.generate(_frame_file(tmp_path), "a dog mascot", 5)
    assert Path(path).parent == tmp_path
    assert Path(path).name.startswith("kling_")
    assert Path(path).suffix == ".mp4"
    assert Path(path).read_bytes() == b"FAKE-KLING-MP4"
    # 폴링 횟수: 3회(IN_QUEUE + IN_PROGRESS + COMPLETED)
    assert len(fake.status_calls) == 3


def test_kling_client_error_status_raises_render_error(tmp_path):
    """status=ERROR면 VideoRenderError를 던진다."""
    fake = FakeKlingHttp(
        get_status_responses=[_Resp(json_data={"status": "ERROR"})],
    )
    client = _kling_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="status=ERROR"):
        client.generate(_frame_file(tmp_path), "prompt", 5)


def test_kling_client_timeout_raises_video_timeout_error(tmp_path):
    """폴링 타임아웃 시 VideoTimeoutError를 던진다(sleep 주입으로 빠르게)."""
    # interval=1.0, timeout=2.0 → 최대 2회 폴링 후 타임아웃
    fake = FakeKlingHttp(
        get_status_responses=[_Resp(json_data={"status": "IN_QUEUE"}) for _ in range(10)],
    )
    client = _kling_client(
        tmp_path,
        fake,
        NUTTI_KLING_POLL_INTERVAL_SEC=1.0,
        NUTTI_KLING_TIMEOUT_SEC=2.0,
    )
    with pytest.raises(VideoTimeoutError) as exc_info:
        client.generate(_frame_file(tmp_path), "prompt", 5)
    assert "폴링" in str(exc_info.value)


def test_kling_client_transient_429_retries_and_succeeds(tmp_path):
    """상태 조회 429 → backoff 재시도 후 성공."""
    sleeps: list[float] = []
    fake = FakeKlingHttp(
        get_status_responses=[
            _Resp(status_code=429),
            _Resp(json_data={"status": "COMPLETED"}),
        ],
    )
    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    client = KlingClient(settings, http=fake, sleep=sleeps.append)
    path = client.generate(_frame_file(tmp_path), "prompt", 5)
    assert Path(path).exists()
    # 429 1회 → 재시도 1회 = 총 2회 폴링
    assert len(fake.status_calls) == 2
    # backoff sleep이 1회 있어야 한다
    assert len(sleeps) >= 1
    assert sleeps[0] > 0


def test_kling_client_transient_500_exhausted_raises(tmp_path):
    """연속 500이 재시도 한도를 초과하면 VideoRenderError로 전파된다."""
    fake = FakeKlingHttp(
        get_status_responses=[_Resp(status_code=500) for _ in range(5)],
    )
    client = _kling_client(tmp_path, fake)
    with pytest.raises(VideoRenderError) as exc_info:
        client.generate(_frame_file(tmp_path), "prompt", 5)
    assert "500" in str(exc_info.value)
    # 최초 1회 + 재시도 3회 = 4회
    assert len(fake.status_calls) == 4


def test_kling_client_submit_missing_request_id_raises(tmp_path):
    """제출 응답에 request_id가 없으면 VideoRenderError를 즉시 던진다."""
    fake = FakeKlingHttp(
        post_response=_Resp(json_data={"other": "field"}),
    )
    client = _kling_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="request_id"):
        client.generate(_frame_file(tmp_path), "prompt", 5)
    # 폴링까지 가지 않아야 한다
    assert len(fake.status_calls) == 0


def test_kling_client_result_missing_video_url_raises(tmp_path):
    """결과 응답에 video.url이 없으면 VideoRenderError를 던진다."""
    fake = FakeKlingHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(json_data={"video": {}}),  # url 없음
    )
    client = _kling_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="URL"):
        client.generate(_frame_file(tmp_path), "prompt", 5)


def test_kling_client_download_no_auth_header_to_cdn(tmp_path):
    """CDN(fal.media) 다운로드 요청에는 Authorization 헤더가 없다(자격증명 격리)."""
    fake = FakeKlingHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/test.mp4"}}
        ),
        download_response=_Resp(content=b"MP4"),
    )
    client = _kling_client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), "prompt", 5)

    assert len(fake.download_calls) == 1
    dl_headers = fake.download_headers[0]
    # CDN 요청에는 Authorization이 없어야 한다
    if dl_headers:
        assert "authorization" not in {k.lower() for k in dl_headers}


def test_kling_client_queue_requests_have_auth_header(tmp_path):
    """큐(queue.fal.run) 요청에는 Authorization: Key 헤더가 포함된다."""
    fake = FakeKlingHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
    )
    client = _kling_client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), "prompt", 5)

    # POST(제출) 헤더 확인
    assert fake.post_headers
    assert "authorization" in {k.lower() for k in (fake.post_headers[0] or {})}
    # 상태 조회 헤더 확인
    assert fake.status_headers
    assert "authorization" in {k.lower() for k in (fake.status_headers[0] or {})}


def test_kling_client_redirect_valid_location_succeeds(tmp_path):
    """다운로드 302 → 허용 호스트(fal.media) Location → 다운로드 성공."""
    redirect_served = {"done": False}

    class _RedirectHttp(FakeKlingHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            # 큐 호스트(상태/결과)는 부모에게 위임
            if "queue.fal.run" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            # 첫 다운로드 요청: 302 반환
            if not redirect_served["done"]:
                redirect_served["done"] = True
                return _Resp(
                    status_code=302,
                    headers={"location": "https://fal.media/clips/final.mp4"},
                )
            return _Resp(content=b"REDIRECTED-MP4")

    fake = _RedirectHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/initial.mp4"}}
        ),
    )
    client = _kling_client(tmp_path, fake)
    path = client.generate(_frame_file(tmp_path), "prompt", 5)
    assert Path(path).read_bytes() == b"REDIRECTED-MP4"


def test_kling_client_redirect_unsafe_location_raises(tmp_path):
    """다운로드 302 → 허용 외 호스트 Location → VideoRenderError(SSRF 방어)."""

    class _EvilRedirectHttp(FakeKlingHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            # 큐 호스트(상태/결과)는 부모에게 위임
            if "queue.fal.run" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            return _Resp(
                status_code=302,
                headers={"location": "https://evil.example.com/steal.mp4"},
            )

    fake = _EvilRedirectHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/trigger.mp4"}}
        ),
    )
    client = _kling_client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.generate(_frame_file(tmp_path), "prompt", 5)


def test_kling_client_chained_redirect_raises(tmp_path):
    """1차 리다이렉트 후 2차 리다이렉트는 거부된다(SSRF 체인 방지)."""
    first_served = {"done": False}

    class _ChainedHttp(FakeKlingHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            # 큐 호스트(상태/결과)는 부모에게 위임
            if "queue.fal.run" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            if not first_served["done"]:
                first_served["done"] = True
                # 1차 리다이렉트 (허용 호스트)
                return _Resp(
                    status_code=302,
                    headers={"location": "https://fal.media/clips/final.mp4"},
                )
            # 2차 리다이렉트 — 거부돼야 한다
            return _Resp(
                status_code=302,
                headers={"location": "https://fal.media/clips/again.mp4"},
            )

    fake = _ChainedHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/trigger.mp4"}}
        ),
    )
    client = _kling_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="추가 리다이렉트"):
        client.generate(_frame_file(tmp_path), "prompt", 5)


def test_kling_client_redirect_missing_location_raises(tmp_path):
    """302 응답에 Location 헤더가 없으면 VideoRenderError."""

    class _NoLocHttp(FakeKlingHttp):
        def get(self, url, *, headers=None, follow_redirects=False):
            # 큐 호스트(상태/결과)는 부모에게 위임
            if "queue.fal.run" in url:
                return super().get(url, headers=headers, follow_redirects=follow_redirects)
            return _Resp(status_code=302, headers={})

    fake = _NoLocHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        get_result_response=_Resp(
            json_data={"video": {"url": "https://fal.media/clips/noloc.mp4"}}
        ),
    )
    client = _kling_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="Location"):
        client.generate(_frame_file(tmp_path), "prompt", 5)


def test_kling_client_poll_count_increments(tmp_path):
    """poll_count는 폴링 HTTP 시도 횟수를 정확히 기록한다."""
    fake = FakeKlingHttp(
        get_status_responses=[
            _Resp(json_data={"status": "IN_QUEUE"}),
            _Resp(json_data={"status": "COMPLETED"}),
        ],
    )
    client = _kling_client(tmp_path, fake)
    client.generate(_frame_file(tmp_path), "prompt", 5)
    assert client.poll_count == 2


def test_kling_client_rejects_nonpositive_interval(tmp_path):
    """kling_poll_interval_sec ≤ 0이면 생성 시점에 ValueError."""
    for bad in (0.0, -1.0):
        settings = _kling_settings(
            NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_KLING_POLL_INTERVAL_SEC=bad
        )
        with pytest.raises(ValueError, match="kling_poll_interval_sec"):
            KlingClient(settings, http=FakeKlingHttp(), sleep=_no_sleep)


def test_kling_client_rejects_nonpositive_timeout(tmp_path):
    """kling_timeout_sec ≤ 0이면 생성 시점에 ValueError."""
    for bad in (0.0, -1.0):
        settings = _kling_settings(
            NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_KLING_TIMEOUT_SEC=bad
        )
        with pytest.raises(ValueError, match="kling_timeout_sec"):
            KlingClient(settings, http=FakeKlingHttp(), sleep=_no_sleep)


def test_kling_client_download_empty_content_raises(tmp_path):
    """다운로드 응답 바이트가 비면 VideoRenderError."""
    fake = FakeKlingHttp(
        get_status_responses=[_Resp(json_data={"status": "COMPLETED"})],
        download_response=_Resp(status_code=200, content=b""),
    )
    client = _kling_client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.generate(_frame_file(tmp_path), "prompt", 5)


def test_kling_client_close_closes_http(tmp_path):
    """close()가 주입된 HTTP 클라이언트를 닫는다."""
    fake = FakeKlingHttp()
    client = _kling_client(tmp_path, fake)
    client.close()
    assert fake.closed is True


# ═══════════════════════════════════════════════════════════════════
# 섹션 C. SSRF / 입력 검증 순수 함수
# ═══════════════════════════════════════════════════════════════════


def test_validate_fal_video_url_allows_fal_media():
    """fal.media HTTPS URL은 통과한다."""
    _validate_fal_video_url("https://fal.media/clips/video.mp4")


def test_validate_fal_video_url_allows_subdomain():
    """fal.media 서브도메인(cdn.fal.media)도 허용한다."""
    _validate_fal_video_url("https://cdn.fal.media/clips/video.mp4")


def test_validate_fal_video_url_allows_fal_run():
    """fal.run HTTPS URL도 허용한다."""
    _validate_fal_video_url("https://fal.run/output/video.mp4")


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://fal.media/clips/video.mp4",        # http → 거부
        "https://evil.example.com/video.mp4",      # 타 호스트
        "ftp://fal.media/clips/video.mp4",         # ftp scheme
        "https://notfal.media/video.mp4",          # fal.media로 끝나지만 not-fal
        "",
    ],
)
def test_validate_fal_video_url_rejects_unsafe(bad_url):
    """허용 외 scheme·호스트는 VideoRenderError."""
    with pytest.raises(VideoRenderError):
        _validate_fal_video_url(bad_url)


def test_validate_request_id_allows_valid():
    """영숫자·`-`·`_`만 있는 ID는 그대로 반환한다."""
    result = _validate_request_id("req-abc_123-XYZ")
    assert result == "req-abc_123-XYZ"


@pytest.mark.parametrize(
    "bad_id",
    [
        "",                      # 빈 문자열
        "   ",                   # 공백만
        "../etc/passwd",         # 경로 순회
        "a" * 129,               # 길이 초과
        "req?inject=1",          # 쿼리 주입
        "req/path/../../evil",   # 슬래시(허용 문자 아님)
    ],
)
def test_validate_request_id_rejects_malformed(bad_id):
    """허용 외 문자 / 빈 값 / 과길이는 VideoRenderError."""
    with pytest.raises(VideoRenderError):
        _validate_request_id(bad_id)


def test_validate_model_id_allows_valid():
    """허용 형태의 모델 경로는 그대로 반환한다."""
    mid = _validate_model_id("fal-ai/kling-video/v2.1/standard/image-to-video")
    assert mid == "fal-ai/kling-video/v2.1/standard/image-to-video"


@pytest.mark.parametrize(
    "bad_mid",
    [
        "",
        "   ",
        "model id with spaces",
        "model?param=val",
        "model:8080/path",
        "a" * 257,
    ],
)
def test_validate_model_id_rejects_malformed(bad_mid):
    """공백·`?`·`:`·과길이 → VideoRenderError."""
    with pytest.raises(VideoRenderError):
        _validate_model_id(bad_mid)


# ═══════════════════════════════════════════════════════════════════
# 섹션 D. GeminiTtsClient
# ═══════════════════════════════════════════════════════════════════


def test_tts_synthesize_success_saves_wav_returns_path_and_duration(tmp_path):
    """정상 합성: WAV 파일 저장 + (경로, 길이초) 반환. 길이 계산 검증."""
    pcm = _make_pcm(num_samples=2400, rate=24000)
    fake = FakeTtsHttp(response=_tts_audio_response(pcm, rate=24000))
    client = _tts_client(tmp_path, fake)
    path, duration = client.synthesize("안녕하세요")
    assert Path(path).parent == tmp_path
    assert Path(path).suffix == ".wav"
    # 길이 = (바이트수 / 2) / rate = 4800 / 2 / 24000 = 0.1초
    expected_duration = (len(pcm) / 2) / 24000
    assert abs(duration - expected_duration) < 1e-6
    # 실제 WAV 파일 구조 검증
    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000


def test_tts_synthesize_camelcase_inline_data(tmp_path):
    """camelCase inlineData 키도 오디오 파트로 인식한다."""
    pcm = _make_pcm(50)
    fake = FakeTtsHttp(response=_tts_audio_response_camelcase(pcm))
    client = _tts_client(tmp_path, fake)
    path, duration = client.synthesize("테스트")
    assert Path(path).exists()
    assert duration > 0


def test_tts_synthesize_missing_audio_part_raises(tmp_path):
    """오디오 파트 없는 응답은 VideoRenderError(finishReason 없을 때)."""
    fake = FakeTtsHttp(
        response=_Resp(
            json_data={
                "candidates": [{"content": {"parts": [{"text": "텍스트만"}]}}]
            }
        )
    )
    client = _tts_client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.synthesize("안녕하세요")


def test_tts_synthesize_missing_audio_with_finish_reason_raises(tmp_path):
    """finishReason이 있을 때도 VideoRenderError(SAFETY 필터 등)."""
    fake = FakeTtsHttp(
        response=_Resp(
            json_data={
                "candidates": [
                    {"content": {"parts": []}, "finishReason": "SAFETY"}
                ]
            }
        )
    )
    client = _tts_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="finishReason=SAFETY"):
        client.synthesize("안녕하세요")


def test_tts_synthesize_base64_decode_failure_raises(tmp_path):
    """base64 디코드 실패는 VideoRenderError로 승격된다."""
    fake = FakeTtsHttp(
        response=_Resp(
            json_data={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inline_data": {
                                        "mime_type": "audio/L16;rate=24000",
                                        "data": "!!!INVALID_BASE64!!!",
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        )
    )
    client = _tts_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="base64"):
        client.synthesize("테스트")


def test_tts_synthesize_http_error_raises_render_error(tmp_path):
    """HTTP 4xx는 VideoRenderError로 전파된다."""
    fake = FakeTtsHttp(response=_Resp(status_code=400))
    client = _tts_client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.synthesize("안녕하세요")


def test_tts_synthesize_transport_error_raises_render_error(tmp_path):
    """전송 오류(ConnectionError)도 VideoRenderError로 승격된다."""
    fake = FakeTtsHttp(exc=ConnectionError("boom"))
    client = _tts_client(tmp_path, fake)
    with pytest.raises(VideoRenderError):
        client.synthesize("안녕하세요")


def test_tts_synthesize_rate_from_mime_used_in_duration(tmp_path):
    """mimeType의 rate=16000이면 길이 계산에 16000이 사용된다."""
    pcm = _make_pcm(num_samples=1600, rate=16000)
    fake = FakeTtsHttp(response=_tts_audio_response(pcm, rate=16000))
    client = _tts_client(tmp_path, fake)
    path, duration = client.synthesize("테스트")
    expected = (len(pcm) / 2) / 16000
    assert abs(duration - expected) < 1e-6
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == 16000


def test_tts_synthesize_empty_text_raises(tmp_path):
    """빈 텍스트 입력은 VideoRenderError."""
    # 빈 텍스트는 _sanitize_prompt_text 이후 clean=""가 되어 raise
    fake = FakeTtsHttp(response=_tts_audio_response())
    client = _tts_client(tmp_path, fake)
    with pytest.raises(VideoRenderError, match="비어"):
        client.synthesize("   ")


def test_tts_close_closes_http(tmp_path):
    """close()가 HTTP 클라이언트를 닫는다."""
    fake = FakeTtsHttp(response=_tts_audio_response())
    client = _tts_client(tmp_path, fake)
    client.close()
    assert fake.closed is True


@pytest.mark.parametrize(
    "bad_model",
    [
        "model?q=inject",            # 쿼리 주입
        "model id with spaces",      # 공백
        "model:generateContent",     # `:` 스킴/메서드 변조
        "model#frag",                # 프래그먼트
        "",                          # 빈 값
        "   ",                       # 공백만
    ],
)
def test_tts_client_rejects_malformed_model(tmp_path, bad_model):
    """비정상 tts_model은 생성 시점에 VideoRenderError(URL 인젝션 방어).

    `:`·`?`·`#`·공백 등 URL 구조를 변조할 문자를 거부한다. 모델 경로 검증은
    Kling 모델과 동일 계약(영숫자·`.`·`_`·`/`·`-`만 허용)이라 `/`·`.`는 통과한다.
    """
    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_TTS_MODEL=bad_model)
    with pytest.raises(VideoRenderError, match="NUTTI_TTS_MODEL"):
        GeminiTtsClient(settings, http=FakeTtsHttp(), sleep=_no_sleep)


@pytest.mark.parametrize(
    "bad_voice",
    [
        "Kore\nInjected",            # 개행(JSON 본문 변조)
        'Kore"evil',                 # 따옴표
        "../Kore",                   # 슬래시/경로 문자
        "voice?q=1",                 # `?`
        "a" * 65,                    # 길이 초과
        "",                          # 빈 값
        "   ",                       # 공백만
    ],
)
def test_tts_client_rejects_malformed_voice(tmp_path, bad_voice):
    """비정상 tts_voice는 생성 시점에 VideoRenderError(JSON voiceName 변조 방어)."""
    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_TTS_VOICE=bad_voice)
    with pytest.raises(VideoRenderError, match="NUTTI_TTS_VOICE"):
        GeminiTtsClient(settings, http=FakeTtsHttp(), sleep=_no_sleep)


def test_tts_client_uses_validated_model_in_url(tmp_path):
    """검증된 tts_model이 합성 요청 URL에 그대로 들어간다(설정값 경유 확인)."""
    fake = FakeTtsHttp(response=_tts_audio_response())
    client = _tts_client(tmp_path, fake, NUTTI_TTS_MODEL="gemini-2.5-flash-preview-tts")
    client.synthesize("안녕하세요")
    url = fake.post_calls[0][0]
    assert "/models/gemini-2.5-flash-preview-tts:generateContent" in url


def test_tts_client_allows_valid_voice_with_space(tmp_path):
    """영숫자·공백·`-`·`_`로 된 정상 음성 이름은 통과하고 voiceName에 쓰인다."""
    fake = FakeTtsHttp(response=_tts_audio_response())
    client = _tts_client(tmp_path, fake, NUTTI_TTS_VOICE="Kore Voice-1")
    client.synthesize("안녕하세요")
    body = fake.post_calls[0][1]
    voice = body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"][
        "voiceName"
    ]
    assert voice == "Kore Voice-1"


# ═══════════════════════════════════════════════════════════════════
# 섹션 D-추가. _parse_pcm_rate / _pcm_to_wav_bytes
# ═══════════════════════════════════════════════════════════════════


def test_parse_pcm_rate_standard():
    """'audio/L16;rate=24000' → 24000."""
    assert _parse_pcm_rate("audio/L16;rate=24000") == 24000


def test_parse_pcm_rate_with_other_params():
    """다른 파라미터가 있어도 rate를 파싱한다."""
    assert _parse_pcm_rate("audio/L16;codec=pcm;rate=48000") == 48000


def test_parse_pcm_rate_missing_falls_back_to_default():
    """rate= 없으면 기본값 24000을 반환한다."""
    assert _parse_pcm_rate("audio/L16") == kling_module._DEFAULT_TTS_RATE
    assert _parse_pcm_rate("") == kling_module._DEFAULT_TTS_RATE


def test_parse_pcm_rate_none_falls_back():
    """None이 들어오면 기본값을 반환한다."""
    assert _parse_pcm_rate(None) == kling_module._DEFAULT_TTS_RATE  # type: ignore[arg-type]


def test_pcm_to_wav_bytes_valid_wav_structure():
    """_pcm_to_wav_bytes가 유효한 WAV 바이너리를 만든다(wave로 재검증)."""
    pcm = b"\x00\x00" * 240  # 240샘플 × 2바이트
    wav_bytes = _pcm_to_wav_bytes(pcm, 24000)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
        assert wf.getnframes() == 240


def test_pcm_to_wav_bytes_riff_header():
    """반환된 바이트가 WAV RIFF 헤더로 시작한다."""
    wav_bytes = _pcm_to_wav_bytes(b"\x00\x00" * 10, 24000)
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"


# ═══════════════════════════════════════════════════════════════════
# 섹션 E. 순수 헬퍼
# ═══════════════════════════════════════════════════════════════════


def test_pick_clip_duration_short_audio_maps_to_5():
    """음성 ≤ 5초 → 클립 5초."""
    assert _pick_clip_duration(0.0) == 5
    assert _pick_clip_duration(3.5) == 5
    assert _pick_clip_duration(5.0) == 5


def test_pick_clip_duration_medium_audio_maps_to_10():
    """5 < 음성 ≤ 10초 → 클립 10초."""
    assert _pick_clip_duration(5.1) == 10
    assert _pick_clip_duration(8.0) == 10
    assert _pick_clip_duration(10.0) == 10


def test_pick_clip_duration_long_audio_caps_at_10():
    """음성 > 10초 → 클립 10초로 cap."""
    assert _pick_clip_duration(12.0) == 10
    assert _pick_clip_duration(60.0) == 10


# ═══════════════════════════════════════════════════════════════════
# 섹션 F. KlingVoiceoverBackend.produce_beat_clips
# ═══════════════════════════════════════════════════════════════════


def test_produce_beat_clips_calls_tts_and_kling_per_beat(monkeypatch, tmp_path):
    """비트 N개마다 tts.synthesize → kling.generate → _mux 순서로 호출된다."""
    kling = FakeKlingClient()
    tts = FakeTtsClient(duration=4.0)  # 4초 → _pick_clip_duration → 5초
    mux_calls: list[tuple[str, str]] = []

    def fake_mux(self, v, a):
        mux_calls.append((v, a))
        return f"data/fake/beat_{len(mux_calls)}.mp4"

    monkeypatch.setattr(KlingVoiceoverBackend, "_mux", fake_mux)

    beats = ["비트1", "비트2", "비트3"]
    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = KlingVoiceoverBackend(settings, kling_client=kling, tts_client=tts)
    clips, total_sec = backend.produce_beat_clips("data/frame.jpg", beats)

    assert len(clips) == 3
    # 비트 3개 × min(clip_dur=5, audio_sec=4.0) = 12.0초 실측 합계
    assert total_sec == pytest.approx(12.0)
    assert len(tts.calls) == 3
    assert len(kling.calls) == 3
    assert len(mux_calls) == 3
    # 순서: 비트 텍스트가 TTS로 전달됐는지 확인
    assert tts.calls == ["비트1", "비트2", "비트3"]
    # kling.generate에 frame_path가 매번 동일하게 전달됐는지
    assert all(c[0] == "data/frame.jpg" for c in kling.calls)
    # clip_dur = _pick_clip_duration(4.0) = 5
    assert all(c[2] == 5 for c in kling.calls)


def test_produce_beat_clips_returns_correct_order(monkeypatch, tmp_path):
    """반환된 클립 경로가 비트 순서와 일치한다."""
    call_order: list[int] = []
    beat_count = [0]

    def fake_mux(self, v, a):
        beat_count[0] += 1
        n = beat_count[0]
        call_order.append(n)
        return f"data/fake/beat_{n}.mp4"

    monkeypatch.setattr(KlingVoiceoverBackend, "_mux", fake_mux)

    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = KlingVoiceoverBackend(
        settings,
        kling_client=FakeKlingClient(),
        tts_client=FakeTtsClient(),
    )
    clips, _total = backend.produce_beat_clips("frame.jpg", ["A", "B", "C"])
    assert clips == ["data/fake/beat_1.mp4", "data/fake/beat_2.mp4", "data/fake/beat_3.mp4"]
    assert call_order == [1, 2, 3]


def test_produce_beat_clips_owned_clients_closed_on_success(monkeypatch, tmp_path):
    """클라이언트 미주입 시 자체 생성 후 finally에서 close가 정확히 1회 호출된다."""
    created: dict = {}

    class _OwnedKling(FakeKlingClient):
        def __init__(self, settings, **kwargs):
            super().__init__()
            created["kling"] = self

    class _OwnedTts(FakeTtsClient):
        def __init__(self, settings, **kwargs):
            super().__init__()
            created["tts"] = self

    monkeypatch.setattr(kling_module, "KlingClient", _OwnedKling)
    monkeypatch.setattr(kling_module, "GeminiTtsClient", _OwnedTts)
    monkeypatch.setattr(KlingVoiceoverBackend, "_mux", lambda self, v, a: "data/beat.mp4")

    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = KlingVoiceoverBackend(settings)
    backend.produce_beat_clips("frame.jpg", ["비트1"])

    assert created["kling"].close_count == 1
    assert created["tts"].close_count == 1


def test_produce_beat_clips_owned_clients_closed_on_failure(monkeypatch, tmp_path):
    """중간에 예외가 발생해도 자체 생성 클라이언트는 finally에서 close된다."""
    created: dict = {}

    class _OwnedKling(FakeKlingClient):
        def __init__(self, settings, **kwargs):
            super().__init__()
            created["kling"] = self

        def generate(self, frame_path, prompt, duration_sec):
            raise VideoRenderError("Kling 실패")

    class _OwnedTts(FakeTtsClient):
        def __init__(self, settings, **kwargs):
            super().__init__()
            created["tts"] = self

    monkeypatch.setattr(kling_module, "KlingClient", _OwnedKling)
    monkeypatch.setattr(kling_module, "GeminiTtsClient", _OwnedTts)

    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = KlingVoiceoverBackend(settings)
    with pytest.raises(VideoRenderError):
        backend.produce_beat_clips("frame.jpg", ["비트1"])

    assert created["kling"].close_count == 1
    assert created["tts"].close_count == 1


def test_produce_beat_clips_injected_clients_not_closed(monkeypatch, tmp_path):
    """주입된 클라이언트는 KlingVoiceoverBackend가 close하지 않는다(소유권 없음)."""
    kling = FakeKlingClient()
    tts = FakeTtsClient()

    monkeypatch.setattr(KlingVoiceoverBackend, "_mux", lambda self, v, a: "data/beat.mp4")

    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = KlingVoiceoverBackend(settings, kling_client=kling, tts_client=tts)
    backend.produce_beat_clips("frame.jpg", ["비트1"])

    # 주입된 클라이언트는 호출부가 닫는다 — backend가 닫으면 안 된다
    assert kling.close_count == 0
    assert tts.close_count == 0


def test_produce_beat_clips_mux_receives_kling_and_tts_paths(monkeypatch, tmp_path):
    """_mux에 전달되는 인자는 kling.generate가 반환한 경로 + tts.synthesize가 반환한 경로다."""
    mux_args: list[tuple[str, str]] = []

    def fake_mux(self, video_path, audio_path):
        mux_args.append((video_path, audio_path))
        return "data/beat.mp4"

    monkeypatch.setattr(KlingVoiceoverBackend, "_mux", fake_mux)

    kling = FakeKlingClient(video_path="data/silent.mp4")
    tts = FakeTtsClient(audio_path="data/voice.wav")

    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = KlingVoiceoverBackend(settings, kling_client=kling, tts_client=tts)
    backend.produce_beat_clips("frame.jpg", ["비트1"])

    assert len(mux_args) == 1
    video_arg, audio_arg = mux_args[0]
    assert video_arg == "data/silent.mp4"
    assert audio_arg == "data/voice.wav"


# ═══════════════════════════════════════════════════════════════════
# 섹션 F-추가. KlingVoiceoverBackend._mux (ffmpeg 우회)
# ═══════════════════════════════════════════════════════════════════


def test_mux_invokes_ffmpeg_with_correct_args(tmp_path, monkeypatch):
    """_mux가 ffmpeg를 -i video -i audio -shortest 인자로 호출한다."""
    import subprocess as _sp

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(_sp, "run", fake_run)

    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = KlingVoiceoverBackend(settings)
    out = backend._mux("silent.mp4", "voice.wav")
    assert out.endswith(".mp4")
    cmd = captured["cmd"]
    assert "-i" in cmd
    assert "silent.mp4" in cmd
    assert "voice.wav" in cmd
    assert "-shortest" in cmd
    assert "-c:v" in cmd
    assert "copy" in cmd
    assert "-c:a" in cmd
    assert "aac" in cmd


def test_mux_ffmpeg_failure_raises_render_error(tmp_path, monkeypatch):
    """ffmpeg 실패 시 VideoRenderError로 변환되고 stderr 원문은 노출되지 않는다."""
    import subprocess as _sp

    def fake_run(cmd, **kw):
        raise _sp.CalledProcessError(1, cmd, stderr=b"ffmpeg-secret-stderr")

    monkeypatch.setattr(_sp, "run", fake_run)

    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = KlingVoiceoverBackend(settings)
    with pytest.raises(VideoRenderError) as exc_info:
        backend._mux("silent.mp4", "voice.wav")
    assert "ffmpeg-secret-stderr" not in str(exc_info.value)
    assert "CalledProcessError" in str(exc_info.value) or "mux" in str(exc_info.value)


def test_mux_os_error_raises_render_error(tmp_path, monkeypatch):
    """ffmpeg 미설치(OSError) 시도 VideoRenderError."""
    import subprocess as _sp

    def fake_run(cmd, **kw):
        raise OSError("ffmpeg not found")

    monkeypatch.setattr(_sp, "run", fake_run)

    settings = _kling_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    backend = KlingVoiceoverBackend(settings)
    with pytest.raises(VideoRenderError, match="OSError"):
        backend._mux("silent.mp4", "voice.wav")
