# gui_cleaner.py
# Streamlit + Gmail cleaner (Cloud-safe)
# Works locally and on Streamlit Community Cloud.

import os
import json
import pickle
import tempfile
from typing import Optional, List

import streamlit as st
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ==============================
# Config
# ==============================
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Detect Streamlit Cloud runtime
IS_CLOUD = bool(os.environ.get("STREAMLIT_RUNTIME"))

# Token location: /tmp on Cloud (ephemeral) | local file otherwise
TOKEN_FILE = (
    os.path.join(tempfile.gettempdir(), "gmail_token.pickle")
    if IS_CLOUD
    else "token.pickle"
)

# Optional OpenAI scoring (falls back to heuristic if not set)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

_client = None
_use_legacy_openai = False
if OPENAI_API_KEY:
    try:
        from openai import OpenAI  # new SDK
        _client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        try:
            import openai  # legacy SDK
            openai.api_key = OPENAI_API_KEY
            _use_legacy_openai = True
        except Exception:
            _client = None
            _use_legacy_openai = False

# ==============================
# Creds loader: Secrets -> temp file | local credentials.json
# ==============================
def resolve_credentials_file() -> Optional[str]:
    """Return a path to a valid Google OAuth client JSON."""
    # 1) Local file next to script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_path = os.path.join(script_dir, "credentials.json")
    if os.path.exists(local_path):
        return local_path

    # 2) Streamlit Secrets (google_oauth)
    data = st.secrets.get("google_oauth", None) if hasattr(st, "secrets") else None
    if data:
        try:
            parsed = json.loads(data) if isinstance(data, str) else dict(data)
            fd, path = tempfile.mkstemp(prefix="google_creds_", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(parsed, f)
            return path
        except Exception as e:
            st.error(f"Invalid google_oauth secret: {e}")
            return None

    return None

# ==============================
# Gmail helpers
# ==============================
def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not getattr(creds, "valid", False):
        if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            creds.refresh(Request())
        else:
            creds_path = resolve_credentials_file()
            if not creds_path:
                st.error(
                    "Google OAuth credentials not found.\n\n"
                    "- Local: put `credentials.json` next to `gui_cleaner.py`, or\n"
                    "- Cloud: in **Settings → Secrets**, add key `google_oauth` with the JSON value."
                )
                st.stop()

            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            # Cloud cannot open localhost; use console auth. Local uses browser.
            if IS_CLOUD:
                creds = flow.run_console()
            else:
                creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)

def list_messages(service, label_id: Optional[str], max_results: int) -> List[dict]:
    kwargs = {"userId": "me", "maxResults": max_results}
    if label_id:
        kwargs["labelIds"] = [label_id]
    resp = service.users().messages().list(**kwargs).execute()
    return resp.get("messages", []) or []

def get_snippet(service, msg_id: str) -> str:
    data = service.users().messages().get(userId="me", id=msg_id, format="minimal").execute()
    return data.get("snippet", "")

def trash_message(service, msg_id: str):
    return service.users().messages().trash(userId="me", id=msg_id).execute()

# ==============================
# Scoring (OpenAI optional)
# ==============================
def cheap_fallback_score(snippet: str) -> float:
    s = snippet.lower()
    hits = 0
    for k in [
        "unsubscribe","promo","promotion","sale","deal","limited time","newsletter",
        "marketing","advertisement","no-reply","noreply","casino","viagra","winner",
        "congratulations","act now"
    ]:
        if k in s:
            hits += 1
    return min(1.0, 0.15 * hits)

def ai_deletion_score(snippet: str) -> float:
    if not OPENAI_API_KEY or (_client is None and not _use_legacy_openai):
        return cheap_fallback_score(snippet)

    prompt = (
        "You are an email cleaning assistant. Given the email snippet, reply "
        "with a single float between 0 and 1 indicating the probability this "
        "email is clutter (promotions/ads/spam) that can be deleted.\n"
        "Reply with ONLY the number.\n\n"
        f"SNIPPET:\n{snippet}\n"
    )

    try:
        if _use_legacy_openai:
            import openai  # type: ignore
            resp = openai.ChatCompletion.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            text = resp.choices[0].message["content"].strip()
        else:
            resp = _client.chat.completions.create(  # type: ignore[attr-defined]
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            text = resp.choices[0].message.content.strip()  # type: ignore
        return float(text)
    except Exception:
        return cheap_fallback_score(snippet)

# ==============================
# UI
# ==============================
st.set_page_config(page_title="Email Cleaner Agent", layout="wide")
st.title("📧 Email Cleaner Agent")
st.caption("Gmail cleaner that scores clutter and can auto-trash it. Cloud-safe auth.")

# Sidebar preflight
st.sidebar.header("Preflight")
st.sidebar.write("Runtime:", "Streamlit Cloud" if IS_CLOUD else "Local")
st.sidebar.write("Token file:", TOKEN_FILE)

preview_mode = st.checkbox("Preview mode (do NOT delete)", value=True)
max_emails = st.slider("How many emails to scan?", 1, 100, 25)
label_to_scan = st.text_input("Gmail label ID (blank = INBOX)", value="INBOX")
threshold = st.slider("Delete threshold", 0.0, 1.0, 0.80, 0.01)

col1, col2 = st.columns(2)
with col1:
    if st.button("🔄 Revoke Session (delete token)"):
        try:
            os.remove(TOKEN_FILE)
            st.success("Token removed. You will re-auth next run.")
        except FileNotFoundError:
            st.info("No token found.")

with col2:
    if st.button("🔍 Scan Inbox Now"):
        with st.spinner("Authenticating with Gmail..."):
            service = get_gmail_service()

        st.markdown("### ✉ Inbox Scan Results")
        messages = list_messages(service, label_to_scan.strip() or None, max_emails)
        if not messages:
            st.info("No messages found.")
        else:
            progress = st.progress(0)
            total = len(messages)
            for i, msg in enumerate(messages, start=1):
                try:
                    snippet = get_snippet(service, msg["id"])
                except Exception as e:
                    st.warning(f"Could not load snippet for {msg.get('id')}: {e}")
                    progress.progress(min(1.0, i/total))
                    continue

                score = ai_deletion_score(snippet)
                st.write(f"**Snippet:** {snippet}")
                st.write(f"**Clutter Score:** `{score:.2f}`  | **Threshold:** `{threshold:.2f}`")

                if score >= threshold:
                    if preview_mode:
                        st.warning("[PREVIEW] Would delete this email.")
                    else:
                        try:
                            trash_message(service, msg["id"])
                            st.error("Deleted (moved to Trash).")
                        except Exception as e:
                            st.warning(f"Failed to delete: {e}")
                else:
                    st.success("Keeping this email.")

                st.markdown("---")
                progress.progress(min(1.0, i/total))

