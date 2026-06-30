"""짧은 대본 라이브 1편 검증 — 적응 무음 트림(_trim_to_speech) 실발동 확인용 (수동 전용).

배경(2026-06-29 PO 피드백): 끝/막바지 글리치를 적응 트림으로 잡으려면 발화가 8초보다
일찍 끝나 뒤에 무음이 남아야 한다. 그런데 batch_video_test.py 대본은 비트당 44~50자라
발화가 8초를 꽉 채워(무음 0) 적응 트림이 미발동 → 글리치 잔존·검증 불가였다.

이 스크립트는 비트당 34~40자 **짧은 대본**으로 라이브 1편을 생성하고, 생성 전후
data/media/veo_fal_trim_*.mp4 개수를 비교해 적응 트림이 실제로 발동했는지, 최종 길이가
얼마나 짧아졌는지(=끝 글리치 구간 제거)를 보고한다. 게시는 하지 않는다.

비용: 1편 ≈ $1.64. 실 API 호출 — CI/테스트 금지.

사용법:
  ./.venv/Scripts/python.exe scripts/smoke_video_short.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video import VideoStudio
from nutti.models import Script

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 비트당 34~40자(공백·구두점 포함) — 발화가 ~7초에 끝나 뒤 무음이 남도록 의도.
# CTA(마지막 비트)는 차분한 권유체(외침·느낌표 금지) + 브랜드명 미언급.
SHORT_SCRIPT = Script(
    topic="강아지 하루 물 마시는 양",
    body="물을 잘 안 마시는 우리 아이, 그냥 두면 안 돼요.",
    beats=[
        "강아지가 물을 잘 안 마시면 건강에 빨간불이 켜질 수 있어요.",
        "강아지 하루 적정 음수량은 몸무게 1킬로당 오십 밀리리터 정도예요.",
        "사료를 물에 살짝 불려주거나 급수기를 놓아두면 훨씬 잘 마셔요.",
        "우리 아이한테 맞는 하루 물 권장량은 프로필 링크에서 확인해보세요.",
    ],
)


def _ffprobe_dur(path: str, ff: str) -> float | None:
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-i", path], capture_output=True
        )
        err = out.stderr.decode("utf-8", "replace")
        import re

        m = re.search(r"Duration:\s*(\d+):(\d+):([0-9.]+)", err)
        if m is None:
            return None
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        return None


def main() -> None:
    s = Settings()
    media = Path(s.nutti_media_dir)
    media.mkdir(parents=True, exist_ok=True)

    print(
        f"[설정] dry_run={s.dry_run}  backend={s.video_backend}  "
        f"fal={'O' if s.fal_key else 'X'}  endframe_lock={s.veo_fal_endframe_lock}  "
        f"tail_trim={s.veo_fal_clip_tail_trim_sec}s  seed={s.veo_fal_seed}"
    )
    if s.dry_run:
        raise SystemExit("dry_run=True — .env에서 NUTTI_DRY_RUN=false로 바꿔야 실제 생성됩니다.")
    if not s.fal_key:
        raise SystemExit("FAL_KEY 없음 — .env에 FAL_KEY를 설정해야 합니다.")

    for i, b in enumerate(SHORT_SCRIPT.beats, 1):
        print(f"  비트{i}: {len(b)}자  {b}")

    trims_before = {p.name for p in media.glob("veo_fal_trim_*.mp4")}

    studio = VideoStudio(s)
    t0 = time.monotonic()
    asset = studio.produce(SHORT_SCRIPT)
    dt = time.monotonic() - t0

    trims_after = {p.name for p in media.glob("veo_fal_trim_*.mp4")}
    new_trims = sorted(trims_after - trims_before)

    vp = asset.video_path
    size_mb = Path(vp).stat().st_size / 1_000_000 if vp and Path(vp).exists() else 0.0

    try:
        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ff = None

    final_dur = _ffprobe_dur(vp, ff) if (ff and vp) else None

    print("\n===== 결과 =====")
    print(f"  소요 {dt:.1f}s  asset.duration={asset.duration_sec}s  "
          f"실측길이={final_dur}s  {size_mb:.2f}MB")
    print(f"  최종 파일: {vp}")
    print(f"\n  적응 트림 발동: {len(new_trims)}개 (비트별 발화끝 트림 — 0개면 미발동=글리치 잔존)")
    for name in new_trims:
        d = _ffprobe_dur(str(media / name), ff) if ff else None
        print(f"    - {name}  ({d}s, 원본 8s에서 트림됨)")
    print(
        "\n  판정 가이드: 트림이 비트마다 발동(4개 근처)하고 각 트림 길이가 8s보다 작으면"
        "\n  발화 끝 기준으로 끝 글리치 구간이 제거된 것. PO가 영상을 보고 ①끝부분 글리치 제거"
        "\n  ②대사 잘림 없음 ③막바지 완전정지를 직접 확인 필요."
    )


if __name__ == "__main__":
    main()
