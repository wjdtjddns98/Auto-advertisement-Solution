"""Veo 3.1 extend 엔드투엔드 라이브 실측 (수동 실행 전용, 유료 ~$1.5).

목적: URI 계약 수정(2026-06-15)이 실 API에서 끝까지 동작하는지 확인한다. 실측 2차
(_diag_veo_extend2.py)로 extend 입력 필드는 `instances[].video.uri`(Files API URI 참조)
로 확정됐으나, "generate 완료 응답이 돌려준 URI를 그대로 extend 입력으로 넣으면 실제로
이어지는가"와 "비트 경계에서 목소리가 끊김 없이 이어지는가"는 유료 호출로만 판정 가능.

흐름(새 코드 경로 그대로):
  1) client._generate_uri(frame, build_beat) -> 첫 8초 클립 생성, Files API URI 반환(다운로드 X)
  2) client.extend(uri1, build_extend_beat) -> 그 URI를 입력으로 +7초 연장, 새 누적 URI 반환
  3) client._download(uri2) -> 최종 누적(약 15초) 영상만 1회 다운로드

extend는 Fast/Standard만 지원하므로 모델을 fast로 강제한다(.env가 lite여도 init kwargs 우선).
기존 프레임 PNG(data/media/frame_*.png)를 재사용해 NanoBanana 비용을 아낀다.
실 API 과금이 있으므로 CI/테스트에서 실행 금지. 출력은 cp949(Windows 콘솔) 호환 문자만.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from nutti.config import Settings
from nutti.integrations.video import VeoClient, VeoPromptBuilder

BEAT1 = "안녕하세요, 누띠예요. 오늘은 우리 강아지 간식 얘기를 해볼게요."
BEAT2 = "그래서 하루에 딱 두 개면 충분하답니다."


def main() -> None:
    # init kwargs가 .env보다 우선 - .env가 Lite여도 여기서 Fast로 강제(extend 필수).
    s = Settings(NUTTI_VEO_MODEL="veo-3.1-fast-generate-preview")
    print(f"[설정] dry_run={s.dry_run}  model={s.veo_model}  "
          f"gemini={'O' if s.gemini_api_key else 'X'}")
    if s.dry_run:
        raise SystemExit("dry_run=True 상태 - .env NUTTI_DRY_RUN=false여야 실제 호출됩니다.")
    if "lite" in s.veo_model.lower():
        raise SystemExit("extend는 Lite 미지원 - fast/standard 모델이어야 합니다.")

    frames = sorted(
        Path(s.nutti_media_dir).glob("frame_*.png"), key=lambda p: p.stat().st_size
    )
    if not frames:
        raise SystemExit(f"재사용할 시작 프레임이 없습니다 - {s.nutti_media_dir}/frame_*.png 확인.")
    frame = frames[0]
    print(f"[시작프레임] {frame.name}  ({frame.stat().st_size/1_000_000:.2f} MB)")

    builder = VeoPromptBuilder()
    client = VeoClient(s)
    t0 = time.monotonic()
    try:
        print("\n[1단계] 첫 클립 generate (8초, image-to-video) - URI 수신(다운로드 없음)")
        uri1 = client._generate_uri(str(frame), builder.build_beat(BEAT1))
        print(f"  uri1 = {uri1[:90]}...")

        print("\n[2단계] extend (uri1 입력, +7초) - 누적 URI 수신")
        uri2 = client.extend(uri1, builder.build_extend_beat(BEAT2))
        print(f"  uri2 = {uri2[:90]}...")

        print("\n[3단계] 최종 누적 영상 1회 다운로드")
        out_path = client._download(uri2)
    finally:
        client.close()
    dt = time.monotonic() - t0

    print("\n===== extend 체이닝 성공 =====")
    print(f"  소요시간 : {dt:.1f}s")
    print(f"  산출영상 : {out_path}")
    if Path(out_path).exists():
        size_mb = Path(out_path).stat().st_size / 1_000_000
        print(f"  파일크기 : {size_mb:.2f} MB (존재 확인 OK)")
    print("\n[육안 판정] 1) 첫 8초 뒤 +7초가 컷 없이 이어진 누적 영상인지 "
          "2) 비트 경계에서 목소리/발화가 끊김 없이 이어지는지 확인.")
    print("  URI 체이닝 계약 라이브 검증 OK - 실 generate URI가 extend 입력으로 수락됨.")
    print(json.dumps({"ok": True, "out": str(out_path)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
