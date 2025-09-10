import streamlit as st
import random, string, datetime, time, json, hashlib
import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# --- Optional: copy-to-clipboard (safe fallback if not installed)
try:
    from st_copy_to_clipboard import st_copy_to_clipboard
except Exception:
    def st_copy_to_clipboard(*args, **kwargs):
        pass

# --- Page Configuration ---
st.set_page_config(page_title="LLM Assessment — Evidence-Based Workflow", layout="wide")

# --- Constants ---
SPREADSHEET_KEY = "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ"  # llmassessment
EVENT_HEADERS = [
    "timestamp","user_id","assignment_id","turn_count","event_type","milestone_id",
    "attachment_type","prompt","response","prompt_len","response_len",
    "latency_ms","prompt_sha256","response_sha256","flags"
]

ASSIGNMENT = {
    "id": "AFM_2025_CW1",
    "title": "Investment Appraisal Coursework",
    "milestones": [
        {"id": "M0", "name": "Plan (aims & approach)", "require_note": True},
        {"id": "M1", "name": "Concept checks (Q&A)", "require_note": True},
        {"id": "M2", "name": "Outline (claims–evidence)", "require_note": True},
        {"id": "M3", "name": "First draft", "require_note": False},
        {"id": "M4", "name": "Critical revisions (why/what)", "require_note": True},
        {"id": "M5", "name": "References & integrity note", "require_note": True},
        {"id": "M6", "name": "Final synthesis (150 words)", "require_note": True},
    ],
}

# --- Helpers ---
def generate_short_id(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

# --- Secrets & Clients ---
try:
    genai.configure(api_key=st.secrets["google_api"]["gemini_api_key"])
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
except Exception as e:
    st.error(f"Gemini setup failed. Check [google_api] in secrets. Error: {e}")
    st.stop()

try:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_KEY)
except Exception as e:
    st.error(f"Google Sheets access failed. Share the sheet with your service account. Error: {e}")
    st.stop()

def get_or_create_ws(title: str, headers: list | None = None):
    """Get a worksheet by title, or create it (with headers if provided)."""
    try:
        ws = sh.worksheet(title)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1, cols=(len(headers) if headers else 10))
        if headers:
            ws.append_row(headers, value_input_option="USER_ENTERED")
    return ws

def get_events_ws():
    return get_or_create_ws("events", EVENT_HEADERS)

def get_connectivity_ws():
    return get_or_create_ws("connectivity", ["timestamp","user_id","note"])

events_ws = get_events_ws()
connectivity_ws = get_connectivity_ws()

# --- Session State ---
if "user_id" not in st.session_state:
    st.session_state.user_id = generate_short_id()
if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0
if "milestone_index" not in st.session_state:
    st.session_state.milestone_index = 0
if "events_cache" not in st.session_state:
    st.session_state.events_cache = []
if "evidence_json" not in st.session_state:
    st.session_state.evidence_json = None

# --- Styles (compact chat bubbles) ---
st.markdown("""
<style>
.chat-msg {padding:0.8rem 1rem;border-radius:12px;margin:0.4rem 0;max-width:80%;}
.chat-user {background:#DCF8C6;margin-left:auto;text-align:right;}
.chat-assistant {background:#F1F0F0;margin-right:auto;text-align:left;}
</style>
""", unsafe_allow_html=True)

# --- Logging ---
def append_event_row(row: list):
    try:
        events_ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        st.warning(f"Log append failed: {e}")

def emit_event(event: dict):
    """Cache locally and write minimal columns to Google Sheets."""
    st.session_state.events_cache.append(event)
    row = [
        event.get("timestamp"),
        event.get("user_id"),
        event.get("assignment_id"),
        event.get("turn_count", 0),
        event.get("event_type"),
        event.get("milestone_id", ""),
        event.get("attachment_type", ""),
        event.get("prompt", ""),
        event.get("response", ""),
        len(event.get("prompt", "") or ""),
        len(event.get("response", "") or ""),
        event.get("latency_ms", 0),
        sha256(event.get("prompt", "")),
        sha256(event.get("response", "")),
        event.get("flags", ""),
    ]
    append_event_row(row)

# --- Connectivity Test ---
def run_connectivity_test():
    """Writes a ping row to the 'connectivity' worksheet and returns row count after write."""
    try:
        ts = datetime.datetime.now().isoformat()
        connectivity_ws.append_row([ts, st.session_state.user_id, "ping"], value_input_option="USER_ENTERED")
        rows = len(connectivity_ws.get_all_values())
        return True, ts, rows
    except Exception as e:
        return False, str(e), None

# --- LLM call ---
def stream_gemini(prompt_text: str) -> str:
    start = time.time()
    chunks = []
    try:
        for chunk in gemini_model.generate_content([prompt_text], stream=True):
            if getattr(chunk, "text", None):
                chunks.append(chunk.text)
    except Exception as e:
        chunks.append(f"Error calling Gemini: {e}")
    latency_ms = round((time.time() - start) * 1000)
    return "".join(chunks), latency_ms

# --- Evidence Pack ---
def build_evidence_pack():
    payload = {
        "version": "1.0",
        "assignment": ASSIGNMENT,
        "student": {"pseudonymous_id": st.session_state.user_id},
        "events": st.session_state.events_cache,
        "created_at": datetime.datetime.now().isoformat(),
    }
    b = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    payload["sha256"] = hashlib.sha256(b).hexdigest()
    return json.dumps(payload, ensure_ascii=False, indent=2)

# --- UI Helpers ---
def milestone_header():
    m = ASSIGNMENT["milestones"][st.session_state.milestone_index]
    st.subheader(f"{ASSIGNMENT['title']} — {m['id']} · {m['name']}")
    st.progress((st.session_state.milestone_index + 1) / len(ASSIGNMENT["milestones"]))
    with st.expander("Academic integrity & fair use of LLMs", expanded=False):
        st.markdown(
            "- Use the assistant to **understand, plan, and refine**.\n"
            "- Keep a record of **what you accepted/rejected and why**.\n"
            "- Your **Evidence Pack** (JSON) shows your process; include a short integrity note."
        )

# --- Sidebar ---
with st.sidebar:
    st.markdown(f"**User ID:** `{st.session_state.user_id}`")
    if st.button("🔧 Run Google Sheets write test", use_container_width=True):
        ok, info, rows = run_connectivity_test()
        if ok:
            st.success(f"Test write succeeded at {info}. Total rows now: {rows}.")
        else:
            st.error(f"Test write failed: {info}")

    with st.expander("Diagnostics", expanded=False):
        st.write("Service account:", st.secrets["gcp_service_account"].get("client_email", "N/A"))
        st.write("Spreadsheet key:", SPREADSHEET_KEY)

    if st.session_state.evidence_json:
        st.download_button(
            "📥 Download Evidence Pack (JSON)",
            data=st.session_state.evidence_json,
            file_name=f"evidence_{ASSIGNMENT['id']}_{st.session_state.user_id}.json",
            mime="application/json",
            use_container_width=True,
        )

# --- Main UI ---
st.title("LLM Assessment — Evidence-Based Workflow")

milestone_header()
m = ASSIGNMENT["milestones"][st.session_state.milestone_index]

# Reflection (if required)
if m["require_note"]:
    note = st.text_area("Brief note for this step (1–3 sentences):", key=f"note_{m['id']}")
    if note:
        emit_event({
            "timestamp": datetime.datetime.now().isoformat(),
            "user_id": st.session_state.user_id,
            "assignment_id": ASSIGNMENT["id"],
            "turn_count": st.session_state.turn_count,
            "event_type": "reflection",
            "milestone_id": m["id"],
            "attachment_type": "text",
            "prompt": "",
            "response": note,
            "latency_ms": 0,
            "flags": "",
        })

# Chat
user_prompt = st.chat_input("Ask for feedback, clarification, or suggestions for this milestone…")
if user_prompt:
    st.session_state.turn_count += 1
    # Log prompt
    emit_event({
        "timestamp": datetime.datetime.now().isoformat(),
        "user_id": st.session_state.user_id,
        "assignment_id": ASSIGNMENT["id"],
        "turn_count": st.session_state.turn_count,
        "event_type": "prompt",
        "milestone_id": m["id"],
        "attachment_type": "text",
        "prompt": user_prompt,
        "response": "",
        "latency_ms": 0,
        "flags": "",
    })

    with st.container():
        st.markdown(f'<div class="chat-msg chat-user">{user_prompt}</div>', unsafe_allow_html=True)
        reply, latency_ms = stream_gemini(user_prompt)
        st.markdown(f'<div class="chat-msg chat-assistant">{reply}</div>', unsafe_allow_html=True)
        st_copy_to_clipboard(reply, "Copy response")

    # Log response
    emit_event({
        "timestamp": datetime.datetime.now().isoformat(),
        "user_id": st.session_state.user_id,
        "assignment_id": ASSIGNMENT["id"],
        "turn_count": st.session_state.turn_count,
        "event_type": "llm_response",
        "milestone_id": m["id"],
        "attachment_type": "text",
        "prompt": user_prompt,
        "response": reply,
        "latency_ms": latency_ms,
        "flags": "",
    })

# Draft workspace
draft = st.text_area("Working draft for this milestone:", height=220, key=f"draft_{m['id']}")
cols = st.columns(3)
with cols[0]:
    if st.button("Save draft snapshot"):
        emit_event({
            "timestamp": datetime.datetime.now().isoformat(),
            "user_id": st.session_state.user_id,
            "assignment_id": ASSIGNMENT["id"],
            "turn_count": st.session_state.turn_count,
            "event_type": "edit",
            "milestone_id": m["id"],
            "attachment_type": "text",
            "prompt": "",
            "response": draft,
            "latency_ms": 0,
            "flags": "",
        })
        st.success("Snapshot saved.")

with cols[1]:
    disabled_prev = st.session_state.milestone_index == 0
    if st.button("⬅️ Previous", disabled=disabled_prev):
        if st.session_state.milestone_index > 0:
            st.session_state.milestone_index -= 1
            st.rerun()

with cols[2]:
    if st.button("Mark milestone complete ✅"):
        emit_event({
            "timestamp": datetime.datetime.now().isoformat(),
            "user_id": st.session_state.user_id,
            "assignment_id": ASSIGNMENT["id"],
            "turn_count": st.session_state.turn_count,
            "event_type": "milestone_submit",
            "milestone_id": m["id"],
            "attachment_type": "",
            "prompt": "",
            "response": "",
            "latency_ms": 0,
            "flags": "",
        })
        if st.session_state.milestone_index < len(ASSIGNMENT["milestones"]) - 1:
            st.session_state.milestone_index += 1
            st.rerun()

# Evidence Pack generation
st.divider()
if st.button("Generate Evidence Pack (JSON)"):
    st.session_state.evidence_json = build_evidence_pack()
    st.success("Evidence Pack generated — use the sidebar button to download.")
