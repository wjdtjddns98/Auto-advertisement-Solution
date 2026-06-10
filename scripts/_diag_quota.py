"""1회 진단 호출: Gemini 이미지 모델의 429 응답에서 quota 항목만 추출(키·본문 비노출)."""
import httpx
from nutti.config import Settings

s = Settings()
model = s.gemini_image_model
url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
headers = {"x-goog-api-key": s.gemini_api_key, "Content-Type": "application/json"}
body = {"contents": [{"parts": [{"text": "a small dog, simple test"}]}]}

print(f"[diag] model={model}  key_len={len(s.gemini_api_key)}")
try:
    r = httpx.post(url, headers=headers, json=body, timeout=30)
    print(f"[diag] status={r.status_code}")
    if r.status_code != 200:
        err = r.json().get("error", {})
        print(f"[diag] code={err.get('code')} status={err.get('status')}")
        print(f"[diag] message={err.get('message')}")
        if not err.get("details"):
            print(f"[diag] raw(앞500자)={r.text[:500]}")
        for d in err.get("details", []):
            t = d.get("@type", "")
            if "QuotaFailure" in t:
                for v in d.get("violations", []):
                    print(f"[diag] QUOTA quotaId={v.get('quotaId')} metric={v.get('quotaMetric')}")
                    dims = v.get("quotaDimensions", {})
                    if dims:
                        print(f"[diag]   dims={dims}")
            elif "RetryInfo" in t:
                print(f"[diag] retryDelay={d.get('retryDelay')}")
            elif "ErrorInfo" in t:
                print(f"[diag] reason={d.get('reason')} domain={d.get('domain')}")
                md = d.get("metadata", {})
                if md:
                    print(f"[diag]   metadata={md}")
    else:
        print("[diag] 200 OK — 한도 정상(이미지 응답 수신). RPD 소진 아님.")
except Exception as e:
    print(f"[diag] EXC {type(e).__name__}: {e}")
