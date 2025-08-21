# gui_cleaner.py
# Email Cleaner Agent – Streamlit GUI
# Run: streamlit run gui_cleaner.py

import os
import pickle
from typing import Optional, List

import streamlit as st
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ==============================
# Config
# ==============================
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_FILE = "token.pickle"

# Where to find Google OAuth client JSON.
# Default: a file named 'credentials.json' in the SAME DIR as this script.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.environ.get(
    "GMAIL_CREDENTIALS_FILE",
    os.path.join(SCRIPT_DIR, "credentials.json"),
)

# OpenAI (optional) — if not set, we use a cheap heuristic fallback
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # or gpt-3.5-turbo

# ==============================
# OpenAI client (best-effort)
# ==============================
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
# Streamlit UI Setup
# ==============================
st.set_page_config(page_title="Email Cleaner Agent", layout="wide")
st.title("📧 Email Cleaner Agent")
st.caption("Connect to Gmail, score inbox clutter, and (optionally) auto-trash it.")

# ---- Sidebar Preflight ----
st.sidebar.header("Preflight")
st.sidebar.write("Working directory:", os.getcwd())
st.sidebar.write("Script directory:", SCRIPT_DIR)
st.sidebar.write("Credentials path:", os.path.abspath(CREDENTIALS_FILE))

if not os.path.exists(CREDENTIALS_FILE):
    st.sidebar.error(
        "Missing Google OAuth client file.\n\n"
        f"Expected here:\n`{os.path.abspath(CREDENTIALS_FILE)}`\n\n"
        "Fix one of these:\n"
        "1) Put credentials.json next to gui_cleaner.py\n"
        "2) Or export GMAIL_CREDENTIALS_FILE=/full/path/credentials.json"
    )
    st.stop()

# ==============================
# Helpers
# ==============================
def get_gmail_service():
    """Authenticate and return a Gmail API service."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not getattr(creds, "valid", False):
        if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            # Will open a browser for consent on first run
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("gmail", "v1", credentials=creds)

def list_messages(service, label_id: Optional[str], max_results: int) -> List[dict]:
    """List messages with optional label (default INBOX if blank)."""
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

def cheap_fallback_score(snippet: str) -> float:
    """Heuristic score 0..1 if no OpenAI key/client is set."""
    s = snippet.lower()
    hits = 0
    keywords = [
        "unsubscribe","promo","promotion","sale","deal","limited time",
        "earnings","casino","viagra","act now","winner","congratulations",
        "newsletter","marketing","advertisement","no-reply","noreply"
    ]
    for k in keywords:
        if k in s:
            hits += 1
    return min(1.0, 0.15 * hits)

def ai_deletion_score(snippet: str) -> float:
    """Return a float [0..1] meaning likelihood of clutter."""
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
# Main Controls
# ==============================
preview_mode = st.checkbox("Preview mode (do NOT delete)", value=True)
max_emails = st.slider("How many emails to scan?", 1, 100, 25)
label_to_scan = st.text_input("Gmail label ID (blank = INBOX)", value="INBOX")
threshold = st.slider("Delete threshold (0.0–1.0)", 0.0, 1.0, 0.80, 0.01)

col_a, col_b = st.columns([1,1], gap="large")
with col_a:
    if st.button("🔄 Revoke Session (delete token)"):
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
            st.success("Deleted token. Re-run and re-auth will be required.")
        else:
            st.info("No token file found. Nothing to delete.")

with col_b:
    if st.button("🔍 Scan Inbox Now"):
        with st.spinner("Authenticating with Gmail..."):
            service = get_gmail_service()

        st.markdown("### ✉ Inbox Scan Results")
        messages = list_messages(service, label_to_scan.strip() or None, max_emails)
        if not messages:
            st.info("No messages found.")
        else:
            progress = st.progress(0)
            for idx, msg in enumerate(messages, start=1):
                try:
                    snippet = get_snippet(service, msg["id"])
                except Exception as e:
                    st.warning(f"Could not load snippet for {msg.get('id')}: {e}")
                    progress.progress(min(1.0, idx/len(messages)))
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
                progress.progress(min(1.0, idx/len(messages)))
