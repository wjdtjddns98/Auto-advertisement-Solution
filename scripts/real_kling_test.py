"""Kling 보이스오버 백엔드 실연동 1편 생성 테스트 (수동 실행 전용).

NUTTI_DRY_RUN=false + NUTTI_VIDEO_BACKEND=kling + FAL_KEY/GEMINI_API_KEY 설정 상태에서
실제로 프레임→무음 Kling 클립×N→한국어 TTS 보이스오버→ffmpeg 스티칭까지 돌려
산출물 경로/길이를 출력한다. 비용이 드는 실 API를 호출하므로 CI/테스트에서 실행 금지.
"""

from __future__ import annotations

import time
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video import VideoStudio
from nutti.models import Script

# 비용 최소화: 검증은 짧은 2비트만(주 검증 대상 = 입 움직임 연출).
# 대사는 PR #46 대본 규칙을 따른다: 훅으로 시작(밋밋한 인사 금지) + 마지막 비트(CTA)에
# 브랜드명('Nutti'·'누띠') 언급 금지 + 비트당 45자 이내.
SCRIPT = Script(
    topic="강아지 간식 급여량, 제대로 알고 주기",
    body="강아지 간식, 대부분 잘못 주고 있다는 사실.",
    beats=[
        "강아지 간식, 대부분 잘못 주고 있다는 거 아세요?",
        "프로필 링크 간식계산기로 우리 아이 맞춤량을 확인하세요!",
    ],
)


def main() -> None:
    s = Settings()
    print(f"[설정] dry_run={s.dry_run}  backend={s.video_backend}  "
          f"fal={'O' if s.fal_key else 'X'}  gemini={'O' if s.gemini_api_key else 'X'}")
    if s.dry_run:
        raise SystemExit("dry_run=True 상태 — .env에서 NUTTI_DRY_RUN=false로 바꿔야 실제 생성됩니다.")

    studio = VideoStudio(s)
    t0 = time.monotonic()
    asset = studio.produce(SCRIPT)
    dt = time.monotonic() - t0

    print("\n===== 생성 완료 =====")
    print(f"  소요시간   : {dt:.1f}s")
    print(f"  프레임     : {asset.frame_image_path}")
    print(f"  영상       : {asset.video_path}")
    print(f"  길이(초)   : {asset.duration_sec}")
    vp = asset.video_path
    if vp and Path(vp).exists():
        size_mb = Path(vp).stat().st_size / 1_000_000
        print(f"  파일크기   : {size_mb:.2f} MB  (존재 확인 ✅)")
    else:
        print("  ⚠️  영상 파일이 디스크에 없음 — 경로 확인 필요")


if __name__ == "__main__":
    main()
