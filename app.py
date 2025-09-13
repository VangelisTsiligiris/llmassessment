import os, io, json, time, datetime, hashlib, random, string
import streamlit as st

# ---------- Optional libs ----------
try:
    import gspread
    from gspread.exceptions import WorksheetNotFound
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    WorksheetNotFound = Exception
    Credentials = None

try:
    import google.generativeai as genai
except Exception:
    genai = None

try:
    import docx  # python-docx
    DOCX_OK = True
except Exception:
    DOCX_OK = False

# ---------- Config ----------
st.set_page_config(page_title="LLM Coursework Helper", layout="wide")

APP_PASSCODE = os.getenv("APP_PASSCODE") or st.secrets.get("env", {}).get("APP_PASSCODE")
if APP_PASSCODE:
    st.session_state.setdefault("__auth_ok", False)
    if not st.session_state["__auth_ok"]:
        st.title("Pilot access")
        code = st.text_input("Enter passcode", type="password")
        if st.button("Enter"):
            if code == APP_PASSCODE:
                st.session_state["__auth_ok"] = True
                st.rerun()
            else:
                st.error("Wrong passcode.")
        st.stop()

SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY", "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ")
ASSIGNMENT_DEFAULT = os.getenv("ASSIGNMENT_ID", "GENERIC")
SIM_THRESHOLD = float(os.getenv("SIM_THRESHOLD", "0.85"))

# ---------- Helpers ----------
def excerpt(text, n=300):
    return text if not text or len(text) <= n else text[:n] + " …"

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

# ---------- Secrets ----------
def load_secrets():
    google_api = st.secrets.get("google_api", {})
    gemini_key = google_api.get("gemini_api_key") or os.getenv("GEMINI_API_KEY")
    sa_info = st.secrets.get("gcp_service_account") or os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    if isinstance(sa_info, str):
        try:
            sa_info = json.loads(sa_info)
        except Exception:
            st.error("Invalid service account JSON")
            st.stop()
    return gemini_key, sa_info

GEMINI_KEY, SA_INFO = load_secrets()

# ---------- Clients ----------
if genai is None or not GEMINI_KEY:
    st.error("Gemini client not available or missing API key.")
    st.stop()
try:
    genai.configure(api_key=GEMINI_KEY)
    LLM = genai.GenerativeModel("gemini-1.5-flash")
except Exception as e:
    st.error(f"Gemini setup failed: {e}")
    st.stop()

if gspread is None or SA_INFO is None or Credentials is None:
    st.error("Google Sheets client not available.")
    st.stop()
try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(SA_INFO, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_KEY)
except Exception as e:
    st.error(f"Google Sheets access failed: {e}")
    st.stop()

def _get_or_create_ws(title, headers):
    try:
        return sh.worksheet(title)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws

EVENTS_WS = _get_or_create_ws("events", ["timestamp","user_id","assignment_id","turn_count","event_type","prompt","response"])
DRAFTS_WS = _get_or_create_ws("drafts", ["user_id","assignment_id","draft_text","last_updated"])
SUBMIS_WS = _get_or_create_ws("submissions", ["timestamp","user_id","assignment_id","word_count","char_count","final_sha256"])

def append_row_safe(ws, row):
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        st.warning(f"Append failed: {e}")

# ---------- Session ----------
if "user_id" not in st.session_state:
    st.session_state.user_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
if "assignment_id" not in st.session_state:
    st.session_state.assignment_id = ASSIGNMENT_DEFAULT
if "chat" not in st.session_state:
    st.session_state.chat = []
if "llm_outputs" not in st.session_state:
    st.session_state.llm_outputs = []
if "final_text" not in st.session_state:
    st.session_state.final_text = ""

# ---------- Core ----------
def ask_llm(prompt_text: str):
    start = time.time()
    chunks = []
    try:
        for ch in LLM.generate_content([prompt_text], stream=True):
            if getattr(ch, "text", None):
                chunks.append(ch.text)
    except Exception as e:
        chunks.append(f"Error: {e}")
    latency = round((time.time() - start) * 1000)
    return "".join(chunks), latency

def save_progress(user_id, assignment_id, draft_text, chat):
    row = [user_id, assignment_id, draft_text, datetime.datetime.now().isoformat()]
    append_row_safe(DRAFTS_WS, row)

def load_progress(user_id, assignment_id):
    try:
        records = DRAFTS_WS.get_all_records()
        for r in records[::-1]:
            if r["user_id"] == user_id and r["assignment_id"] == assignment_id:
                return r["draft_text"]
    except Exception:
        return None
    return None

def export_evidence_docx(user_id, assignment_id, chat, final_text):
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")
    d = docx.Document()
    d.add_heading("Coursework Evidence Report", 0)
    d.add_paragraph(f"User ID: {user_id}")
    d.add_paragraph(f"Assignment ID: {assignment_id}")
    d.add_paragraph(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    d.add_heading("Chat with LLM", level=1)
    for m in chat:
        role = "Student" if m["role"] == "user" else "LLM"
        d.add_paragraph(f"{role}: {m['text']}")

    d.add_heading("Final Draft", level=1)
    d.add_paragraph(final_text)

    buf = io.BytesIO()
    d.save(buf)
    buf.seek(0)
    return buf.read()

# ---------- Sidebar ----------
st.sidebar.write(f"**User ID:** `{st.session_state.user_id}`")
st.sidebar.text_input("Assignment ID", key="assignment_id")

# ---------- Tabs ----------
tab_chat, tab_draft, tab_submit = st.tabs(["💬 Assistant", "📝 Draft", "📊 Evidence & Submit"])

with tab_chat:
    st.header("LLM Assistant")
    for m in st.session_state.chat:
        with st.chat_message(m["role"]):
            st.markdown(m["text"])
    if prompt := st.chat_input("Ask for ideas, critique, examples..."):
        st.session_state.chat.append({"role": "user", "text": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        reply, latency = ask_llm(prompt)
        st.session_state.chat.append({"role": "assistant", "text": reply})
        st.session_state.llm_outputs.append(reply)
        with st.chat_message("assistant"):
            st.markdown(reply)
        append_row_safe(EVENTS_WS, [datetime.datetime.now().isoformat(), st.session_state.user_id,
                                    st.session_state.assignment_id, len(st.session_state.chat),
                                    "chat", prompt, reply])

with tab_draft:
    st.header("Draft your coursework")
    st.session_state.final_text = st.text_area("Write your draft here", height=300,
                                               value=st.session_state.final_text)
    col1, col2 = st.columns(2)
    with col1:
        if st.button("💾 Save draft"):
            save_progress(st.session_state.user_id, st.session_state.assignment_id,
                          st.session_state.final_text, st.session_state.chat)
            st.success("Draft saved to Google Sheets")
    with col2:
        if st.button("🔄 Load last saved draft"):
            loaded = load_progress(st.session_state.user_id, st.session_state.assignment_id)
            if loaded:
                st.session_state.final_text = loaded
                st.experimental_rerun()
            else:
                st.warning("No saved draft found.")

with tab_submit:
    st.header("Evidence & Submission")
    final_text = st.session_state.final_text
    if st.button("📤 Submit"):
        words = len(final_text.split())
        chars = len(final_text)
        append_row_safe(SUBMIS_WS, [datetime.datetime.now().isoformat(),
                                    st.session_state.user_id,
                                    st.session_state.assignment_id,
                                    words, chars,
                                    sha256(final_text)])
        st.success("Submission logged to Google Sheets")

    if st.button("⬇️ Export Evidence as DOCX"):
        try:
            data = export_evidence_docx(st.session_state.user_id,
                                        st.session_state.assignment_id,
                                        st.session_state.chat,
                                        final_text)
            st.download_button("Download DOCX", data=data,
                               file_name=f"evidence_{st.session_state.user_id}.docx",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        except Exception as e:
            st.error(f"Export failed: {e}")
