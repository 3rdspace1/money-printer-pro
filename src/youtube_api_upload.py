"""
Headless YouTube upload via the YouTube Data API v3.

MoneyPrinterPro's stock uploader drives a logged-in Firefox profile with
Selenium. That cannot work on a CI runner: there is no persistent profile, and
Google blocks interactive sign-in from datacenter IPs. This module uploads with
a long-lived OAuth refresh token instead, so `bypass_daily.py --upload-api`
works unattended in GitHub Actions.

Only `requests` is needed.

One-time setup
--------------
1. Google Cloud console: create a project, enable "YouTube Data API v3".
2. Create an OAuth client of type **Desktop app**. Note the client id/secret.
3. Get a refresh token once, on your own machine:

       python -m youtube_api_upload --authorize --client-id ... --client-secret ...

   Follow the printed URL, paste the code back, and store the refresh token.
4. Put the three values in secrets / env:
   YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN.

Quota note: an upload costs ~1600 units of the default 10,000/day quota, so
roughly six uploads a day before you need a quota increase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
CHUNK_SIZE = 8 * 1024 * 1024


class YouTubeUploadError(RuntimeError):
    """Raised when the Data API upload cannot be completed."""


@dataclass
class YouTubeCredentials:
    client_id: str
    client_secret: str
    refresh_token: str

    @classmethod
    def from_env(cls) -> "YouTubeCredentials":
        creds = cls(
            client_id=os.environ.get("YOUTUBE_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip(),
            refresh_token=os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip(),
        )
        missing = [
            name
            for name, value in (
                ("YOUTUBE_CLIENT_ID", creds.client_id),
                ("YOUTUBE_CLIENT_SECRET", creds.client_secret),
                ("YOUTUBE_REFRESH_TOKEN", creds.refresh_token),
            )
            if not value
        ]
        if missing:
            raise YouTubeUploadError(f"Missing env vars: {', '.join(missing)}")
        return creds


def get_access_token(credentials: YouTubeCredentials, timeout: int = 30) -> str:
    """Exchanges the long-lived refresh token for a short-lived access token."""
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise YouTubeUploadError(f"Token refresh failed ({response.status_code}): {response.text[:300]}")
    token = response.json().get("access_token")
    if not token:
        raise YouTubeUploadError("Token refresh returned no access_token.")
    return token


def upload_video(
    video_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy_status: str = "private",
    category_id: str = "25",
    made_for_kids: bool = False,
    credentials: YouTubeCredentials | None = None,
) -> str:
    """
    Uploads `video_path` and returns the new video id.

    `privacy_status` defaults to "private" so an unattended run can never
    publish something unreviewed by accident. Pass "public" once you trust it.
    Category 25 is News & Politics.
    """
    if not os.path.exists(video_path):
        raise YouTubeUploadError(f"Video not found: {video_path}")

    credentials = credentials or YouTubeCredentials.from_env()
    access_token = get_access_token(credentials)
    file_size = os.path.getsize(video_path)

    metadata = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": (tags or [])[:15],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }

    # Step 1: start a resumable session.
    start = requests.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(file_size),
            "X-Upload-Content-Type": "video/*",
        },
        json=metadata,
        timeout=60,
    )
    if start.status_code not in (200, 201):
        raise YouTubeUploadError(f"Could not start upload ({start.status_code}): {start.text[:300]}")

    session_url = start.headers.get("Location")
    if not session_url:
        raise YouTubeUploadError("Upload session started but no Location header was returned.")

    # Step 2: push the file in chunks so a large render does not sit in memory
    # and a dropped connection can be resumed rather than restarted.
    uploaded = 0
    with open(video_path, "rb") as handle:
        while uploaded < file_size:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            end = uploaded + len(chunk) - 1
            response = requests.put(
                session_url,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {uploaded}-{end}/{file_size}",
                },
                data=chunk,
                timeout=600,
            )
            if response.status_code in (200, 201):
                video_id = response.json().get("id", "")
                if not video_id:
                    raise YouTubeUploadError(f"Upload finished but no id returned: {response.text[:200]}")
                return video_id
            if response.status_code == 308:
                uploaded = end + 1
                continue
            raise YouTubeUploadError(f"Chunk upload failed ({response.status_code}): {response.text[:300]}")

    raise YouTubeUploadError("Upload ended without a completion response.")


def _authorize_cli(client_id: str, client_secret: str) -> None:
    """Prints the consent URL and exchanges the pasted code for a refresh token."""
    from urllib.parse import urlencode

    params = {
        "client_id": client_id,
        "redirect_uri": OOB_REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    print("\nOpen this URL, approve access, then paste the code back here:\n")
    print(f"{AUTH_URL}?{urlencode(params)}\n")
    code = input("Code: ").strip()

    response = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": OOB_REDIRECT,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(f"Authorization failed ({response.status_code}): {response.text[:300]}")

    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "No refresh_token returned. Revoke the app at "
            "https://myaccount.google.com/permissions and retry."
        )
    print(f"\nYOUTUBE_REFRESH_TOKEN={refresh_token}\n")
    print("Store that as a repository secret. It does not expire unless revoked.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="YouTube Data API helper")
    parser.add_argument("--authorize", action="store_true", help="Get a refresh token")
    parser.add_argument("--client-id", default=os.environ.get("YOUTUBE_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("YOUTUBE_CLIENT_SECRET", ""))
    args = parser.parse_args()

    if not args.authorize:
        parser.error("Nothing to do. Pass --authorize.")
    if not args.client_id or not args.client_secret:
        parser.error("--client-id and --client-secret are required.")
    _authorize_cli(args.client_id, args.client_secret)
