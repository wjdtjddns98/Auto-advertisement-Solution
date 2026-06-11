"""1회 진단 호출: fal Kling LipSync 422 원인 추출(수동 실행 전용, 키 비노출).

기존 비트 클립 + ElevenLabs WAV를 재사용해 Kling 재생성 비용 없이 LipSync만
제출→폴링→결과 조회하고, 프로덕션이 리댁션하는 에러 본문을 콘솔에만 출력한다.
핵심 확인: 422가 '입력 형식(data URI) 거부'인지 '강아지(비인간) 얼굴 미인식'인지.
"""
import sys
import time
from pathlib import Path

import httpx

from nutti.config import Settings

VIDEO = Path("data/media/beat_e0d4518b77e7.mp4")
AUDIO = Path("data/media/el_voice_76504af3fc73.wav")

s = Settings()
model = s.kling_lipsync_model
app_id = "/".join(model.split("/")[:2])
headers = {"Authorization": f"Key {s.fal_key}", "Content-Type": "application/json"}

print(f"[diag] model={model} app_id={app_id}")
print(f"[diag] video={VIDEO.name}({VIDEO.stat().st_size}B) audio={AUDIO.name}({AUDIO.stat().st_size}B)")


def upload(c: httpx.Client, path: Path, content_type: str) -> str:
    """fal 스토리지에 파일을 올리고 file_url을 반환한다(initiate → presigned PUT)."""
    init = c.post(
        # storage_type=gcs는 이 계정에서 403("not available for this account") → fal-cdn-v3 사용.
        "https://rest.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3",
        headers={**headers, "Accept": "application/json"},
        json={"file_name": path.name, "content_type": content_type},
    )
    print(f"[diag] upload.initiate {path.name} status={init.status_code}")
    if init.status_code != 200:
        print(f"[diag] initiate body(앞500자)={init.text[:500]}")
        sys.exit(1)
    upload_url, file_url = init.json()["upload_url"], init.json()["file_url"]
    put = c.put(upload_url, content=path.read_bytes(), headers={"Content-Type": content_type})
    print(f"[diag] upload.put {path.name} status={put.status_code} file_url_host={file_url.split('/')[2]}")
    if put.status_code not in (200, 201):
        print(f"[diag] put body(앞500자)={put.text[:500]}")
        sys.exit(1)
    return file_url


with httpx.Client(timeout=120.0) as c:
    # 인자로 request_id를 주면 제출을 건너뛰고 기존 작업을 이어서 폴링한다.
    if len(sys.argv) > 1:
        rid = sys.argv[1]
        print(f"[diag] 기존 request_id 재사용={rid}")
    else:
        # data URI는 422(input_value_error: 파일 내용을 읽지 못함) — 스토리지 업로드 후 URL 전달.
        video_url = upload(c, VIDEO, "video/mp4")
        audio_url = upload(c, AUDIO, "audio/wav")
        r = c.post(
            f"https://queue.fal.run/{model}",
            headers=headers,
            json={"video_url": video_url, "audio_url": audio_url},
        )
        print(f"[diag] submit status={r.status_code}")
        if r.status_code != 200:
            print(f"[diag] submit body(앞1000자)={r.text[:1000]}")
            sys.exit(1)
        rid = r.json()["request_id"]
        print(f"[diag] request_id={rid}")

    for _ in range(60):
        st = c.get(f"https://queue.fal.run/{app_id}/requests/{rid}/status", headers=headers)
        # fal 큐 status는 진행 중(IN_QUEUE/IN_PROGRESS)일 때 202, 완료 시 200을 준다.
        status = st.json().get("status") if st.status_code in (200, 202) else None
        print(f"[diag] poll status_code={st.status_code} status={status}")
        if st.status_code not in (200, 202):
            print(f"[diag] poll body(앞1000자)={st.text[:1000]}")
            sys.exit(1)
        if status == "COMPLETED":
            break
        time.sleep(5)

    res = c.get(f"https://queue.fal.run/{app_id}/requests/{rid}", headers=headers)
    print(f"[diag] result status={res.status_code}")
    print(f"[diag] result body(앞2000자)={res.text[:2000]}")
