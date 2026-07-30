"""Seedance 2.0 립싱크 백엔드 단위 테스트.

대상: FalSeedanceClient, VeoPromptBuilder.build_beat_lipsync,
VideoStudio seedance 분기, 비용 계산.
모든 테스트는 fake http/monkeypatch 주입으로 **네트워크·ffmpeg 없이** 동작한다.

섹션 구성:
  A. FalSeedanceClient — TTS·영상 제출 페이로드·폴링·SSRF 방어
  B. build_beat_lipsync — 대사 미포함·@Audio1 참조·하드룰
  C. VideoStudio seedance 분기 — 배선·dry_run·키 검증
  D. 비용 계산 — seedance 단가
"""

from __future__ import annotations

import pytest

from nutti.config import Settings
from nutti.integrations.video import (
    EpisodeStyle,
    VeoPromptBuilder,
    VideoRenderError,
    VideoStudio,
)
from nutti.integrations.video_seedance import FalSeedanceClient
from nutti.models import Script
from nutti.pipeline.cost import estimate_run_cost

_AUDIO_URL = "https://v3b.fal.media/files/b/abc/line.mp3"
_VIDEO_URL = "https://v3b.fal.media/files/b/abc/clip.mp4"


def _seedance_settings(**overrides) -> Settings:
    """seedance 백엔드 설정(실 경로, 키 채워짐, 폴링 즉시)."""
    base: dict = {
        "NUTTI_DRY_RUN": False,
        "FAL_KEY": "test-fal-key",
        "NUTTI_VIDEO_BACKEND": "seedance",
        "NUTTI_VEO_FAL_POLL_INTERVAL_SEC": 1.0,
        "NUTTI_VEO_FAL_TIMEOUT_SEC": 30.0,
    }
    base.update(overrides)
    return Settings(**base)


def _no_sleep(_seconds):
    return None


def _style() -> EpisodeStyle:
    return EpisodeStyle(
        outfit="a plain navy slim-fit tee",
        setting="standing in a bright tidy kitchen",
        prop="",
        fmt="direct",
    )


class _Resp:
    """httpx.Response 대역."""

    def __init__(self, *, status_code: int = 200, json_data=None, content: bytes = b""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.content = content
        self.headers: dict = {}

    def json(self):
        return self._json


class FakeSeedanceHttp:
    """FalSeedanceClient 주입용 fake HTTP.

    URL로 라우팅한다 — TTS(seed-speech)와 영상(seedance-2.0)이 같은 큐 규약을 쓰므로
    제출 페이로드를 각각 캡처해 검증할 수 있게 한다.
    """

    def __init__(self):
        self.tts_payloads: list[dict] = []
        self.video_payloads: list[dict] = []

    def post(self, url, headers=None, json=None, **kwargs):
        if "seed-speech" in url:
            self.tts_payloads.append(json or {})
            return _Resp(json_data={"request_id": "tts-req-1"})
        self.video_payloads.append(json or {})
        return _Resp(json_data={"request_id": "vid-req-1"})

    def get(self, url, headers=None, **kwargs):
        if url.endswith("/status"):
            return _Resp(json_data={"status": "COMPLETED"})
        if "requests/tts-req-1" in url:
            return _Resp(json_data={"audio": {"url": _AUDIO_URL}})
        if "requests/vid-req-1" in url:
            return _Resp(json_data={"video": {"url": _VIDEO_URL}})
        # fal CDN 다운로드
        return _Resp(content=b"FAKE-MEDIA-BYTES")


# ══════════════════════ A. FalSeedanceClient ══════════════════════


def test_tts_returns_cdn_url_and_local_path(tmp_path):
    """TTS는 fal CDN URL(재업로드 불요)과 로컬 파일 경로를 함께 돌려준다."""
    http = FakeSeedanceHttp()
    client = FalSeedanceClient(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)), http=http, sleep=_no_sleep
    )
    url, path = client.tts("딸기 하루 몇 알, 그게 문제야.")
    assert url == _AUDIO_URL
    assert path.endswith(".mp3")
    payload = http.tts_payloads[0]
    assert payload["text"] == "딸기 하루 몇 알, 그게 문제야."
    assert payload["voice"] == "shane_ko"
    # 말투 지시는 기본값이 있으므로 실려야 한다(목소리 톤 통제 수단).
    assert payload["voice_instruction"]
    # pitch 기본 0은 페이로드에서 생략한다.
    assert "pitch" not in payload


def test_tts_rejects_empty_line(tmp_path):
    """빈 대사는 과금 전에 실패한다."""
    client = FalSeedanceClient(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)),
        http=FakeSeedanceHttp(),
        sleep=_no_sleep,
    )
    with pytest.raises(VideoRenderError):
        client.tts("   ")


def test_generate_payload_is_lipsync_shaped(tmp_path):
    """영상 제출은 오디오를 레퍼런스로 넘기고 모델 자체 음성을 끈다."""
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"FAKE-FRAME")
    http = FakeSeedanceHttp()
    client = FalSeedanceClient(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)), http=http, sleep=_no_sleep
    )
    out = client.generate(str(frame), "PROMPT", audio_url=_AUDIO_URL, duration_sec=4.2)
    assert out.endswith(".mp4")
    payload = http.video_payloads[0]
    # 우리 TTS로 목소리를 고정하는 것이 이 백엔드의 존재 이유 — 자체 음성은 꺼야 한다.
    assert payload["generate_audio"] is False
    assert payload["audio_urls"] == [_AUDIO_URL]
    assert payload["image_urls"][0].startswith("data:image/png;base64,")
    assert payload["aspect_ratio"] == "9:16"
    # 4.2초 오디오 → 올림해서 5초(짧게 주면 대사가 잘린다).
    assert payload["duration"] == "5"


@pytest.mark.parametrize(
    ("audio_sec", "expected"),
    [(0.5, "4"), (4.0, "4"), (7.1, "8"), (14.2, "15")],
)
def test_generate_clamps_duration_to_schema_range(tmp_path, audio_sec, expected):
    """duration은 올림하고 하한(4초)까지 올린다(상한 초과는 별 테스트에서 실패 검증)."""
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"FAKE-FRAME")
    http = FakeSeedanceHttp()
    client = FalSeedanceClient(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)), http=http, sleep=_no_sleep
    )
    client.generate(str(frame), "PROMPT", audio_url=_AUDIO_URL, duration_sec=audio_sec)
    assert http.video_payloads[0]["duration"] == expected


def test_result_fetch_retries_intermittent_422(tmp_path):
    """결과 조회의 간헐 422는 재시도로 넘긴다 — 이미 과금된 클립을 조회 한 번에 잃지 않는다."""
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"FAKE-FRAME")

    class Flaky422Http(FakeSeedanceHttp):
        def __init__(self):
            super().__init__()
            self.result_calls = 0

        def get(self, url, headers=None, **kwargs):
            if "requests/vid-req-1" in url and not url.endswith("/status"):
                self.result_calls += 1
                if self.result_calls == 1:
                    return _Resp(status_code=422, json_data={"detail": "transient"})
            return super().get(url, headers=headers, **kwargs)

    http = Flaky422Http()
    client = FalSeedanceClient(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)), http=http, sleep=_no_sleep
    )
    out = client.generate(str(frame), "PROMPT", audio_url=_AUDIO_URL, duration_sec=5.0)
    assert out.endswith(".mp4")
    assert http.result_calls == 2


def test_generate_rejects_non_fal_audio_host(tmp_path):
    """오디오 URL도 fal CDN 호스트만 허용한다(SSRF 방어)."""
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"FAKE-FRAME")
    client = FalSeedanceClient(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)),
        http=FakeSeedanceHttp(),
        sleep=_no_sleep,
    )
    with pytest.raises(VideoRenderError):
        client.generate(
            str(frame), "PROMPT", audio_url="https://evil.example/x.mp3", duration_sec=5.0
        )


# ═════════════════════ B. build_beat_lipsync ═════════════════════


def test_lipsync_prompt_omits_dialogue_and_references_audio():
    """대사는 프롬프트에 들어가지 않고 @Audio1/@Image1로 참조된다."""
    prompt = VeoPromptBuilder().build_beat_lipsync(
        off_screen_interviewer=False, style=_style(), final_cta=False
    )
    assert "@Audio1" in prompt
    assert "@Image1" in prompt
    # 대사를 싣지 않으므로 인용 구분자가 없다(하드룰 expected_quotes=0).
    assert prompt.count("'") == 0
    # 목소리 묘사(_VOICE)는 TTS가 대체하므로 들어가지 않는다.
    assert "vocal fingerprint" not in prompt
    # 화면 텍스트 금지는 유지된다.
    assert "no subtitle bar" in prompt.lower() or "subtitle" in prompt.lower()


# ═════════════════ C. VideoStudio seedance 분기 ═════════════════


class FakeSeedanceClient:
    """VideoStudio 배선 검증용 fake — 호출 인자를 기록한다."""

    def __init__(self, tmp_path):
        self._tmp = tmp_path
        self.tts_calls: list[str] = []
        self.prompts: list[str] = []
        self.durations: list[float] = []
        self.closed = False

    def tts(self, text):
        self.tts_calls.append(text)
        path = self._tmp / f"tts_{len(self.tts_calls)}.mp3"
        path.write_bytes(b"FAKE-MP3")
        return _AUDIO_URL, str(path)

    def generate(self, frame_path, prompt, *, audio_url, duration_sec):
        self.prompts.append(prompt)
        self.durations.append(duration_sec)
        path = self._tmp / f"silent_{len(self.prompts)}.mp4"
        path.write_bytes(b"FAKE-MP4")
        return str(path)

    def close(self):
        self.closed = True


def _patch_ffmpeg_steps(monkeypatch, tmp_path, audio_sec=4.5):
    """ffmpeg 의존 단계(길이측정·머지·스티칭·자막)를 대역으로 바꾼다."""
    monkeypatch.setattr(VideoStudio, "_probe_duration_sec", lambda self, p: audio_sec)

    def fake_mux(self, clip, audio_path):
        out = tmp_path / f"muxed_{len(list(tmp_path.glob('muxed_*.mp4')))}.mp4"
        out.write_bytes(b"MUXED")
        return str(out)

    monkeypatch.setattr(VideoStudio, "_mux_audio", fake_mux)
    monkeypatch.setattr(VideoStudio, "_stitch", lambda self, clips, durs, **kw: clips[0])
    monkeypatch.setattr(VideoStudio, "_burn_captions", lambda self, *a, **kw: None)


def _make_silent_video(tmp_path, seconds: float) -> str:
    """ffmpeg로 무음 테스트 영상을 만든다(번들 의존성 — 다른 테스트도 ffmpeg를 쓴다)."""
    import subprocess

    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    out = tmp_path / f"silent_{seconds}.mp4"
    subprocess.run(
        [ff, "-hide_banner", "-y", "-f", "lavfi", "-i", f"color=c=black:s=64x64:d={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True,
        check=True,
    )
    return str(out)


def _make_audio(tmp_path, seconds: float) -> str:
    """ffmpeg로 테스트 오디오(사인파 mp3)를 만든다."""
    import subprocess

    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    out = tmp_path / f"tone_{seconds}.mp3"
    subprocess.run(
        [ff, "-hide_banner", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         str(out)],
        capture_output=True,
        check=True,
    )
    return str(out)


def _has_audio_stream(path: str) -> bool:
    import subprocess

    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    err = subprocess.run(
        [ff, "-hide_banner", "-i", path], capture_output=True, text=True, errors="replace"
    ).stderr
    return "Audio:" in err


def test_mux_audio_attaches_audio_stream(tmp_path):
    """무음 클립 + 오디오 → 오디오 스트림이 있는 클립(실제 ffmpeg 경로)."""
    studio = VideoStudio(_seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)))
    silent = _make_silent_video(tmp_path, 3.0)
    assert not _has_audio_stream(silent)
    muxed = studio._mux_audio(silent, _make_audio(tmp_path, 2.0))
    assert _has_audio_stream(muxed)
    # -shortest: 짧은 쪽(오디오 2초)에 맞춰진다.
    assert studio._probe_duration_sec(muxed) == pytest.approx(2.0, abs=0.3)


def test_mux_audio_warns_when_speech_truncated(tmp_path, capsys):
    """영상이 오디오보다 짧으면 대사가 잘린 것이므로 경고를 남긴다(조용한 손실 방지).

    structlog는 stdout으로 렌더하므로 caplog가 아니라 capsys로 확인한다.
    """
    studio = VideoStudio(_seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)))
    silent = _make_silent_video(tmp_path, 2.0)
    studio._mux_audio(silent, _make_audio(tmp_path, 5.0))
    assert "speech_truncated" in capsys.readouterr().out


def test_mux_audio_raises_on_ffmpeg_failure(tmp_path):
    """ffmpeg 실패는 조용히 원본을 돌려주지 않고 실패시킨다(무음 편 업로드 방지)."""
    studio = VideoStudio(_seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)))
    with pytest.raises(VideoRenderError):
        studio._mux_audio(str(tmp_path / "does-not-exist.mp4"), _make_audio(tmp_path, 1.0))


def test_generate_rejects_audio_over_duration_cap(tmp_path):
    """오디오가 모델 상한(15초)을 넘으면 조용히 깎지 않고 실패한다."""
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"FAKE-FRAME")
    http = FakeSeedanceHttp()
    client = FalSeedanceClient(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)), http=http, sleep=_no_sleep
    )
    with pytest.raises(VideoRenderError):
        client.generate(str(frame), "PROMPT", audio_url=_AUDIO_URL, duration_sec=18.0)
    # 과금 전에 막아야 한다 — 제출조차 하지 않는다.
    assert http.video_payloads == []


def test_produce_seedance_wires_tts_per_beat(monkeypatch, tmp_path):
    """비트마다 TTS→립싱크 클립을 만들고, 총길이는 TTS 실측 합이다."""
    _patch_ffmpeg_steps(monkeypatch, tmp_path, audio_sec=4.5)
    fake = FakeSeedanceClient(tmp_path)
    studio = VideoStudio(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_CAPTION_BURN=False),
        seedance_client=fake,
    )
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"FAKE-FRAME")
    beats = ["첫 비트야.", "둘째 비트야.", "셋째 비트야."]
    _path, total = studio._produce_clips_seedance(frame_path=str(frame), beats=beats, style=_style())
    assert fake.tts_calls == beats
    assert total == pytest.approx(4.5 * 3)
    # 오디오 실측 길이가 그대로 영상 duration 요청으로 전달된다.
    assert fake.durations == [4.5, 4.5, 4.5]
    # 주입 클라이언트는 VideoStudio가 닫지 않는다(소유하지 않으므로).
    assert fake.closed is False
    # 대사는 프롬프트에 들어가지 않는다(자막 환각 트리거 제거).
    assert all("첫 비트야" not in p for p in fake.prompts)


def test_produce_seedance_fails_loudly_when_audio_length_unknown(monkeypatch, tmp_path):
    """오디오 길이를 못 재면 대사가 잘릴 수 있으므로 조용히 넘기지 않는다."""
    _patch_ffmpeg_steps(monkeypatch, tmp_path)
    monkeypatch.setattr(VideoStudio, "_probe_duration_sec", lambda self, p: None)
    studio = VideoStudio(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path)),
        seedance_client=FakeSeedanceClient(tmp_path),
    )
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"FAKE-FRAME")
    with pytest.raises(VideoRenderError):
        studio._produce_clips_seedance(frame_path=str(frame), beats=["비트"], style=_style())


def test_produce_routes_to_seedance_backend(monkeypatch, tmp_path):
    """video_backend=seedance면 produce가 seedance 경로로 간다."""
    _patch_ffmpeg_steps(monkeypatch, tmp_path, audio_sec=5.0)
    monkeypatch.setattr(VideoStudio, "_generate_frame", lambda self, s, st: str(tmp_path / "f.png"))
    (tmp_path / "f.png").write_bytes(b"FAKE-FRAME")
    fake = FakeSeedanceClient(tmp_path)
    studio = VideoStudio(
        _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path), NUTTI_CAPTION_BURN=False),
        seedance_client=fake,
    )
    asset = studio.produce(Script(topic="딸기", body="본문", beats=["하나.", "둘."]))
    assert len(fake.tts_calls) == 2
    assert asset.duration_sec == pytest.approx(10.0)


def test_validate_config_requires_fal_key_for_seedance():
    """seedance 실 경로는 FAL_KEY가 없으면 시작 시점에 실패한다."""
    studio = VideoStudio(_seedance_settings(FAL_KEY=""))
    with pytest.raises(ValueError):
        studio.validate_config()


def test_dry_run_skips_seedance_calls(tmp_path):
    """dry_run은 네트워크 없이 더미 산출물을 돌려준다(백엔드 무관)."""
    studio = VideoStudio(
        Settings(NUTTI_DRY_RUN=True, NUTTI_VIDEO_BACKEND="seedance"),
        seedance_client=FakeSeedanceClient(tmp_path),
    )
    asset = studio.produce(Script(topic="딸기", body="본문", beats=["하나.", "둘."]))
    assert asset.video_path.startswith("data/dry_run/")


# ═════════════════════════ D. 비용 계산 ═════════════════════════


def test_cost_uses_seedance_unit_price(tmp_path):
    """seedance 백엔드는 실측 단가($0.3025/초·720p)로 집계된다."""
    from nutti.models import PipelineRun, VideoAsset

    settings = _seedance_settings(NUTTI_MEDIA_DIR=str(tmp_path))
    run = PipelineRun(
        topic="딸기",
        script=Script(topic="딸기", body="본문", beats=[]),
        video=VideoAsset(
            script_id="s1",
            frame_image_path="data/media/frame.png",
            video_path="data/media/v.mp4",
            final_url="data/media/v.mp4",
            duration_sec=15.0,
        ),
    )
    cost = estimate_run_cost(run, settings)
    video_items = [item for item in cost.items if "Seedance" in item.label]
    assert len(video_items) == 1
    assert video_items[0].usd == pytest.approx(0.3025 * 15.0)
