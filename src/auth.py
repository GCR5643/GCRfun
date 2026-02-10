"""
Google OAuth2 인증 모듈

Gmail API + Google Tasks API + Google Calendar API에 접근하기 위한 OAuth2 인증을 처리합니다.
최초 실행 시 브라우저를 통해 Google 계정 로그인 후 권한을 승인하면
token.json이 생성되어 이후에는 자동으로 인증됩니다.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# Gmail 수정+작성 + Tasks + Calendar 읽기/쓰기 권한
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


def get_credentials() -> Credentials:
    """OAuth2 인증 정보를 반환합니다. 토큰이 없거나 만료되면 갱신합니다."""
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "config/credentials.json")
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "config/token.json")

    creds = None

    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(creds_path).exists():
                raise FileNotFoundError(
                    f"credentials.json을 찾을 수 없습니다: {creds_path}\n"
                    "Google Cloud Console에서 OAuth 클라이언트를 생성하고\n"
                    "credentials.json을 다운로드하여 config/ 폴더에 넣어주세요.\n"
                    "참고: https://console.cloud.google.com/apis/credentials"
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)

        Path(token_path).parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def get_gmail_service():
    """Gmail API 서비스 객체를 반환합니다."""
    return build("gmail", "v1", credentials=get_credentials())


def get_tasks_service():
    """Google Tasks API 서비스 객체를 반환합니다."""
    return build("tasks", "v1", credentials=get_credentials())


def get_calendar_service():
    """Google Calendar API 서비스 객체를 반환합니다."""
    return build("calendar", "v3", credentials=get_credentials())
