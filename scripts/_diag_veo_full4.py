"""기존 프레임 재사용 Veo extend 풀 4비트 실테스트 (수동 실행 전용, 유료 ~$3).

목적: PO 핵심 검증 = 4비트 extend 체이닝이 "컷 없는 단일 연속 영상 + 비트 경계
음성/발화 연속성"을 실제로 만드는지 육안 판정. 프레임 생성(NanoBanana)이 현재
ReadTimeout으로 막혀 풀 파이프라인이 첫 단계에서 멈추므로, 기존
data/media/frame_*.png 1장을 시작 프레임으로 주입해 _produce_clips(veo extend)만
실 호출한다(프레임 생성 인프라 이슈 우회).

extend는 Fast/Standard만 지원하므로 모델을 fast로 강제한다(.env가 lite여도 init 우선).
실 API 과금이 있으므로 CI/테스트에서 실행 금지. 출력은 cp949(Windows 콘솔) 호환 문자만.
"""

from __future__ import annotations

import time
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video import VideoStudio, pick_episode_style
from nutti.models import Script

# real_video_test.py와 동일 대본(4비트: 훅·핵심·팁·CTA).
SCRIPT = Script(
    topic="강아지 간식 급여량, 제대로 알고 주기",
    body="강아지 간식, 대부분 잘못 주고 있다는 사실.",
    beats=[
        "강아지 간식, 대부분 잘못 주고 있다는 거 아세요?",
        "하루 간식은 권장 칼로리의 10%를 넘기면 안 돼요.",
        "몸무게별 적정량, 계산기로 3초면 확인할 수 있어요.",
        "프로필 링크 간식 계산기로 우리 아이 맞춤량을 확인하세요!",
    ],
)


def main() -> None:
    # init kwargs가 .env보다 우선 - .env가 Lite여도 여기서 Fast로 강제(extend 필수).
    s = Settings(NUTTI_VEO_MODEL="veo-3.1-fast-generate-preview")
    print(f"[설정] dry_run={s.dry_run}  backend={s.video_backend}  "
          f"model={s.veo_model}  gemini={'O' if s.gemini_api_key else 'X'}")
    if s.dry_run:
        raise SystemExit("dry_run=True - .env NUTTI_DRY_RUN=false여야 실제 호출됩니다.")
    if "lite" in s.veo_model.lower():
        raise SystemExit("extend는 Lite 미지원 - fast/standard 모델이어야 합니다.")

    frames = sorted(
        Path(s.nutti_media_dir).glob("frame_*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not frames:
        raise SystemExit(f"재사용할 프레임이 없습니다 - {s.nutti_media_dir}/frame_*.png 확인.")
    frame = frames[0]
    print(f"[시작프레임 재사용] {frame.name}  ({frame.stat().st_size/1_000_000:.2f} MB)")

    studio = VideoStudio(s)
    style = pick_episode_style(SCRIPT.id)
    beats = studio._beats(SCRIPT)
    print(f"[비트] {len(beats)}개  예상길이 {int(8 + 7 * (len(beats) - 1))}초")

    t0 = time.monotonic()
    video_path, duration = studio._produce_clips(str(frame), beats, style)
    dt = time.monotonic() - t0

    print("\n===== extend 체이닝 생성 완료 =====")
    print(f"  소요시간 : {dt:.1f}s")
    print(f"  영상     : {video_path}")
    print(f"  길이(초) : {duration}")
    if video_path and Path(video_path).exists():
        size_mb = Path(video_path).stat().st_size / 1_000_000
        print(f"  파일크기 : {size_mb:.2f} MB (존재 확인 OK)")
    else:
        print("  [경고] 영상 파일이 디스크에 없음 - 경로 확인 필요")
    print("\n[PO 육안판정] 1) 4비트가 컷 없는 단일 연속 영상인지 "
          "2) 비트 경계에서 목소리/발화가 끊김 없이 이어지는지 확인.")


if __name__ == "__main__":
    main()
