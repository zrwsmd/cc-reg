"""Test Graph API email reading"""
import sys
sys.path.insert(0, ".")

from src.services.outlook.account import OutlookAccount
from src.services.outlook.token_manager import TokenManager
from src.services.outlook.base import ProviderType
from curl_cffi import requests

lines = open("outlook_token_backup.txt", encoding="utf-8").readlines()
acct = OutlookAccount(
    email=lines[0].split(": ", 1)[1].strip(),
    password=lines[1].split(": ", 1)[1].strip(),
    client_id=lines[2].split(": ", 1)[1].strip(),
    refresh_token=lines[3].split(": ", 1)[1].strip(),
)

print(f"Email: {acct.email}")
print(f"has_oauth: {acct.has_oauth()}")

# Get Graph API token
tm = TokenManager(acct, ProviderType.GRAPH_API)
token = tm.get_access_token()
print(f"Token: {'OK' if token else 'FAILED'}")
if not token:
    sys.exit(1)

# Read inbox
r = requests.get(
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
    params={
        "$top": "5",
        "$select": "subject,from,receivedDateTime",
        "$orderby": "receivedDateTime desc",
    },
    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    impersonate="chrome110",
    timeout=15,
)

print(f"HTTP: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    msgs = data.get("value", [])
    print(f"Found {len(msgs)} emails in inbox")
    for m in msgs:
        sender = m.get("from", {}).get("emailAddress", {}).get("address", "?")
        subj = m.get("subject", "")
        dt = m.get("receivedDateTime", "")
        print(f"  {dt} | {sender} | {subj[:60]}")
else:
    print(f"Error: {r.text[:500]}")
