"""Safe, read-only Google API smoke test for voice-agent-v3-2.

Put this file next to main.py and run on your own computer:
    uv run python smoke_google_readonly.py --connect-only
    uv run python smoke_google_readonly.py
    uv run python smoke_google_readonly.py --gmail  # only if Gmail READ scope already granted

This program does not create/change/delete calendar events or send email.
The application's existing OAuth flow may request calendar read/write permission,
but this script makes read-only API calls only.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from googleapiclient.discovery import build

from integrations.google_calendar import GoogleCalendarClient
from router.factory import build_agent_router

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def local_path(env_name: str, default: str) -> Path:
    path = Path(os.getenv(env_name, default))
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description="Google OAuth/Calendar/Gmail read-only smoke test")
    parser.add_argument("--connect-only", action="store_true", help="Test browser OAuth without API listing")
    parser.add_argument("--gmail", action="store_true", help="Also list the number of recent Gmail messages, if authorized")
    args = parser.parse_args()

    credentials_path = local_path("GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "credentials/client_secret.json")
    state_path = local_path("GOOGLE_ACCOUNT_STATE_FILE", "data/google_accounts.json")
    timezone_name = os.getenv("GOOGLE_CALENDAR_TIMEZONE", "Europe/Kyiv")
    if not credentials_path.is_file():
        print("ERROR: OAuth Desktop client file not found:", credentials_path)
        print("Download the Desktop app JSON from Google Cloud and save it at that path.")
        return 2

    print("Read-only smoke test: no calendar changes and no emails will be sent.")
    router = build_agent_router(
        client_secrets_file=credentials_path,
        state_file=state_path,
        timezone=timezone_name,
        use_keyring=True,
        shared_device=False,  # personal local smoke test, not a shared kiosk test
    )
    accounts = router.accounts
    status = accounts.status()
    if not status.connected or not status.calendar_ready:
        print("Opening Google sign-in in your browser for Calendar access...")
        attempt = accounts.connect(with_calendar=True, with_gmail=False)
        if not attempt.ok:
            print("OAuth NOT completed:", attempt.message)
            return 3
        status = attempt.status

    print("OAuth connected:", status.connected)
    print("Calendar permission:", status.calendar_ready)
    print("Gmail read permission:", status.gmail_readonly_ready)
    if not status.calendar_ready:
        print("ERROR: Calendar access was not granted.")
        return 4
    if args.connect_only:
        print("PASS: Google sign-in and Calendar scope check completed.")
        return 0

    _, calendar_credentials = accounts.credentials_for(calendar=True)
    client = GoogleCalendarClient(calendar_credentials)
    now = datetime.now(ZoneInfo(timezone_name))
    until = now + timedelta(days=7)
    events = client.list_events(now, until)
    print("Checked calendar: primary")
    print("Configured timezone:", timezone_name)
    print("From:", now.isoformat())
    print("To:  ", until.isoformat())
    print("PASS: Google Calendar responded. Events in next 7 days:", len(events))
    for index, event in enumerate(events, start=1):
        starts = event.get("start") or {}
        ends = event.get("end") or {}
        print(f"\nEvent {index}:")
        print("  Title:", event.get("summary") or "(Untitled event)")
        print("  Starts:", starts.get("dateTime") or starts.get("date") or "(Unknown)")
        print("  Ends:  ", ends.get("dateTime") or ends.get("date") or "(Unknown)")
        print("  Status:", event.get("status") or "(Not supplied)")
    if len(events) == 50:
        print("NOTE: This test requests max 50 events; there may be more.")

    if args.gmail:
        if not status.gmail_readonly_ready:
            print("GMAIL NOT TESTED: Gmail read permission is not granted.")
            print("Grant Gmail permission in the voice app on a separate test account first.")
        else:
            _, gmail_credentials = accounts.credentials_for(gmail_readonly=True)
            gmail = build("gmail", "v1", credentials=gmail_credentials, cache_discovery=False)
            result = gmail.users().messages().list(userId="me", maxResults=5).execute()
            print("PASS: Gmail responded. Returned message IDs:", len(result.get("messages", [])))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Do not dump exception objects, which can sometimes include sensitive API URLs.
        print("FAILED:", type(exc).__name__)
        print("Check Google Cloud API enablement, Test users, OAuth scopes and your network.")
        sys.exit(1)
