"""YouTube OAuth refresh token 재발급 헬퍼 (수동 실행 전용).

`.env`의 YOUTUBE_CLIENT_ID/SECRET를 그대로 써서 OAuth 동의 플로우(로컬 루프백)를
돌리고, **새 refresh token**을 출력한다. 출력값을 `.env`의 YOUTUBE_REFRESH_TOKEN에
붙여넣으면 끝.

왜 필요한가:
    동의화면이 "테스트(Testing)" 상태면 refresh token이 7일 만에 만료된다
    (invalid_grant). 토큰을 다시 발급하면 복구되지만, 영구 해결은 동의화면을
    "프로덕션"으로 게시하는 것이다(그 후 발급분은 자동 만료 없음).

요구 스코프(코드 실사용 기준):
    - youtube.upload          : videos.insert (업로드)
    - yt-analytics.readonly   : Analytics 성과 조회

실행:
    ./.venv/Scripts/python.exe -m scripts.get_youtube_refresh_token

브라우저가 열리면 업로드 대상 계정으로 로그인 → 권한 허용.
"테스트 앱" 경고가 떠도 "고급 > 안전하지 않은 페이지로 이동"으로 진행(본인 앱).
콘솔에 출력된 refresh token을 .env에 반영한다.
"""

from __future__ import annotations

from nutti.config import Settings

# 코드가 실제로 호출하는 API에 대응하는 최소 스코프.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def main() -> None:
    s = Settings()
    client_id = (s.youtube_client_id or "").strip()
    client_secret = (s.youtube_client_secret or "").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "YOUTUBE_CLIENT_ID/SECRET이 .env에 없습니다 — 동의화면 OAuth 클라이언트부터 만드세요."
        )

    # google-auth-oauthlib는 실행 시에만 import(런타임 의존 명확화).
    from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError

    # client_secret.json 파일 없이 .env 값으로 클라이언트 설정을 구성한다.
    # "installed"(Desktop) 타입으로 로컬 루프백 리다이렉트를 사용한다.
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)

    # access_type=offline + prompt=consent 를 강제해야 refresh_token이 매번 발급된다.
    # timeout_seconds: 브라우저를 닫거나 방치하면 무한 대기(좀비)하므로 5분 제한을 둔다.
    try:
        creds = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
            timeout_seconds=300,
            authorization_prompt_message="브라우저에서 권한을 허용하세요. 안 열리면 이 URL을 여세요:\n{url}",
            success_message="인증 완료 — 이 창을 닫고 콘솔로 돌아가세요.",
        )
    except WSGITimeoutError:
        raise SystemExit("5분 내 인증이 완료되지 않았습니다 — 다시 실행하세요.") from None

    if not creds.refresh_token:
        raise SystemExit(
            "refresh_token이 발급되지 않았습니다. 동의화면에서 권한을 새로 허용했는지 확인하세요."
        )

    print("\n" + "=" * 60)
    print("새 YOUTUBE_REFRESH_TOKEN (아래 한 줄을 .env에 붙여넣으세요):")
    print("=" * 60)
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 60)
    print("반영 후: ./.venv/Scripts/python.exe -m scripts.smoke_youtube_upload 로 검증")


if __name__ == "__main__":
    main()
