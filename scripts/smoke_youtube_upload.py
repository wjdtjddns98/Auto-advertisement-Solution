"""YouTube 업로드 실연동 1편 스모크 테스트 (수동 실행 전용).

NUTTI_DRY_RUN=false + YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN 설정 상태에서
영상 제작 단계를 건너뛰고 **업로드만** 실제로 호출한다.
기존 로컬 mp4(기본 data/test_video.mp4)를 그대로 올려 video_id를 확인한다.

안전장치: 첫 검증은 채널에 공개 노출되지 않도록 privacyStatus="private"를 강제한다.
공개로 올리려면 명시적으로 `--public`을 준다(권장하지 않음).

실행 예:
    ./.venv/Scripts/python.exe -m scripts.smoke_youtube_upload
    ./.venv/Scripts/python.exe -m scripts.smoke_youtube_upload --file data/test_video.mp4

실제 업로드(쿼터 ~1600 units 소비)이므로 CI/테스트에서 실행 금지.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.publishing import Publisher
from nutti.models import Metadata, VideoAsset


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube 업로드 스모크 테스트")
    parser.add_argument("--file", default="data/test_video.mp4", help="업로드할 로컬 mp4 경로")
    parser.add_argument(
        "--public",
        action="store_true",
        help="공개(public)로 업로드(기본은 안전하게 private)",
    )
    args = parser.parse_args()

    video_path = Path(args.file)
    if not video_path.is_file():
        raise SystemExit(f"영상 파일이 없습니다: {video_path}")

    # privacy를 명시 강제(.env보다 우선) — 기본 private.
    privacy = "public" if args.public else "private"
    s = Settings(NUTTI_YOUTUBE_PRIVACY_STATUS=privacy)

    print(
        f"[설정] dry_run={s.dry_run}  privacy={s.youtube_privacy_status}  "
        f"client_id={'O' if s.youtube_client_id else 'X'}  "
        f"refresh_token={'O' if s.youtube_refresh_token else 'X'}"
    )
    if s.dry_run:
        raise SystemExit("dry_run=True 상태 - .env에서 NUTTI_DRY_RUN=false로 바꿔야 실제 업로드됩니다.")
    if not (s.youtube_client_id and s.youtube_client_secret and s.youtube_refresh_token):
        raise SystemExit("YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN 중 비어 있는 값이 있습니다.")

    video = VideoAsset(
        script_id="smoke_test",
        video_path=str(video_path),
        final_url=str(video_path),
    )
    meta = Metadata(
        title="[비공개 테스트] Nutti 업로드 연동 확인",
        description="업로드 파이프라인 스모크 테스트입니다. 무시해 주세요.",
        hashtags=["강아지", "간식", "테스트"],
    )

    print(f"[업로드 시작] file={video_path.name}  size="
          f"{video_path.stat().st_size / 1_000_000:.2f} MB")
    publisher = Publisher(s)
    result = publisher.upload_youtube(video, meta)

    print("\n===== 업로드 완료 =====")
    print(f"  platform    : {result.platform}")
    print(f"  video_id    : {result.external_id}")
    print(f"  url         : {result.url}")
    print(f"  privacy     : {s.youtube_privacy_status}")
    print("\n채널 'YouTube Studio > 콘텐츠'에서 비공개 영상으로 확인하세요.")


if __name__ == "__main__":
    main()
