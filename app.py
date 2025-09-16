import os, io, json, time, datetime, hashlib, random, string, html as _html
import streamlit as st
from streamlit_quill import st_quill
from streamlit.components.v1 import html as st_html
import pandas as pd

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
    import docx
    DOCX_OK = True
except Exception:
    DOCX_OK = False

# ---------- Similarity Backend Setup ----------
SIM_BACKEND = "none"
try:
    from sentence_transformers import SentenceTransformer, util as sbert_util
    @st.cache_resource
    def load_sbert_model():
        return SentenceTransformer("all-MiniLM-L6-v2")
    _sbert_model = load_sbert_model()
    SIM_BACKEND = "sbert"
except Exception:
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        SIM_BACKEND = "tfidf"
    except Exception:
        from difflib import SequenceMatcher
        SIM_BACKEND = "difflib"

# ---------- HTML to Text Helper ----------
try:
    from bs4 import BeautifulSoup
    def html_to_text(html: str) -> str:
        return BeautifulSoup(html or "", "html.parser").get_text("\n")
except Exception:
    def html_to_text(html: str) -> str:
        return (html or "").replace("<br>", "\n").replace("<br/>", "\n")

# ---------- Page Config and CSS ----------
st.set_page_config(
    page_title="LLM Coursework Helper",
    layout="wide",
    menu_items={"Get help": None, "Report a bug": None, "About": None},
)

st.markdown("""
<style>
:root {
  --ui-font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, "Noto Sans", "Liberation Sans", sans-serif;
}
html, body, [data-testid="stAppViewContainer"] * { font-family: var(--ui-font) !important; }
.header-bar {display:flex; gap:.75rem; flex-wrap:wrap; font-size:.95rem; color:#444; margin-bottom:.25rem;}
.status-chip{background:#f5f7fb;border:1px solid #e6e9f2;border-radius:999px;padding:.15rem .6rem}
.small-muted{color:#7a7f8a}
.chat-box { height: 420px; overflow-y:auto; border:1px solid #dcdfe6; border-radius:10px; background:#fff; padding:.5rem; }
.chat-empty{ border:1px dashed #e6e9f2; background:#fbfbfb; color:#708090; padding:.6rem .8rem; border-radius:10px; }
.chat-bubble { border-radius:12px; padding:.7rem .9rem; margin:.45rem .2rem; border:1px solid #eee; line-height:1.55; font-size:0.95rem; font-family: var(--ui-font) !important; }
.chat-user      { background:#eef7ff; }
.chat-assistant { background:#f6f6f6; }
.chat-bubble p { margin:.35rem 0; }
.chat-bubble ul, .chat-bubble ol { margin:.35rem 0 .35rem 1.25rem; }
.chat-bubble table { border-collapse:collapse; width:100%; margin:.35rem 0; }
.chat-bubble a { color:#2563eb; text-decoration:none; }
.chat-bubble a:hover { text-decoration:underline; }
.chat-bubble code { background:#f3f4f6; padding:.05rem .25rem; border-radius:4px; }
.chat-bubble pre { background:#111827; color:#f9fafb; padding:.7rem .9rem; border-radius:10px; overflow:auto; font-size:.9rem; }
.landing-container { max-width: 800px; margin: 2rem auto; padding: 2rem; background-color: #fcfdff; border: 1px solid #e6e9f2; border-radius: 10px; }
.landing-container h1 { font-size: 2.5rem; color: #111; }
.landing-container .stButton button { height: 3rem; font-size: 1.1rem; }
</style>
""", unsafe_allow_html=True)


# ---------- GLOBAL SESSION STATE & AUTHENTICATION ----------
def _gen_id(n=6): return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))

APP_PASSCODE = os.getenv("APP_PASSCODE") or st.secrets.get("env", {}).get("APP_PASSCODE")
ACADEMIC_PASSCODE = os.getenv("ACADEMIC_PASSCODE") or st.secrets.get("env", {}).get("ACADEMIC_PASSCODE")

# Initialize session state keys
st.session_state.setdefault("__auth_ok", False)
st.session_state.setdefault("user_id", None)
st.session_state.setdefault("is_academic", False)
st.session_state.setdefault("show_landing_page", True)

# Gatekeeper: check for authentication
if not st.session_state["__auth_ok"]:
    st.title("LLM Coursework Helper Login")
    passcode = st.text_input("Enter Passcode", type="password", label_visibility="collapsed", placeholder="Enter Passcode")
    
    if st.button("Login", use_container_width=True):
        if ACADEMIC_PASSCODE and passcode == ACADEMIC_PASSCODE:
            st.session_state["__auth_ok"] = True
            st.session_state["is_academic"] = True
            st.session_state["user_id"] = "Academic"
            st.success("Logged in as Academic.")
            st.rerun()
        elif APP_PASSCODE and passcode == APP_PASSCODE:
            sid = _gen_id()
            st.session_state["user_id"] = sid
            st.session_state["__auth_ok"] = True
            st.session_state["is_academic"] = False
            st.session_state["show_landing_page"] = True
            st.success(f"Login successful! Your new Student ID is **{sid}**")
            st.info("Please copy this ID somewhere safe to resume your work later.")
            st.rerun()
        else:
            st.error("Passcode is incorrect.")
    st.stop()


# ---------- GLOBAL RESOURCES (Clients & Config) ----------
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY", "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ")
ASSIGNMENT_DEFAULT = os.getenv("ASSIGNMENT_ID", "GENERIC")
SIM_THRESHOLD = float(os.getenv("SIM_THRESHOLD", "0.85"))
AUTO_SAVE_SECONDS = int(os.getenv("AUTO_SAVE_SECONDS", "60"))

@st.cache_resource
def get_gspread_client():
    sa_info_str = os.getenv("GCP_SERVICE_ACCOUNT_JSON") or st.secrets.get("gcp_service_account")
    if not sa_info_str: st.error("GCP Service Account JSON not found in secrets."); st.stop()
    try: sa_info = json.loads(sa_info_str)
    except json.JSONDecodeError: st.error("Invalid GCP Service Account JSON."); st.stop()
    
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_KEY)

@st.cache_resource
def get_llm_client():
    gemini_key = os.getenv("GEMINI_API_KEY") or st.secrets.get("google_api", {}).get("gemini_api_key")
    if not gemini_key: st.error("Gemini API key not found in secrets."); st.stop()
    genai.configure(api_key=gemini_key)
    return genai.GenerativeModel("gemini-1.5-flash")

sh = get_gspread_client()
LLM = get_llm_client()

@st.cache_resource
def _get_or_create_ws(title, headers):
    try: return sh.worksheet(title)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws

EVENTS_WS = _get_or_create_ws("events", ["timestamp", "user_id", "assignment_id", "turn_count", "event_type", "prompt", "response"])
DRAFTS_WS = _get_or_create_ws("drafts", ["user_id", "assignment_id", "draft_html", "draft_text", "last_updated"])


# ---------- GLOBAL HELPER FUNCTIONS (used by both views) ----------
def excerpt(text, n=300):
    t = text or ""
    return t if len(t) <= n else t[:n] + " …"

def md_to_html(text: str) -> str:
    import re, html as _h
    t = _h.escape(text or "")
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\\1</strong>", t)
    return t.replace("\n", "<br>")

def compute_similarity_report(final_text, llm_texts, sim_thresh=SIM_THRESHOLD):
    finals = [p.strip() for p in (final_text or "").split("\n") if p.strip()]
    llm_segs = [s.strip() for t in llm_texts for s in (t or "").split("\n") if s.strip()]
    if not finals or not llm_segs:
        return {"backend": SIM_BACKEND, "mean": 0.0, "high_share": 0.0, "rows": []}
    
    rows, high_tokens = [], 0
    total_tokens = sum(len(s.split()) for s in finals)
    
    if SIM_BACKEND == "sbert":
        Ef = _sbert_model.encode(finals, convert_to_tensor=True, normalize_embeddings=True)
        El = _sbert_model.encode(llm_segs, convert_to_tensor=True, normalize_embeddings=True)
        sims = sbert_util.cos_sim(Ef, El).cpu().numpy()
        for i, fseg in enumerate(finals):
            j = int(sims[i].argmax()); s = float(sims[i, j]); nearest = llm_segs[j]
            rows.append({"final_seg": excerpt(fseg, 200), "nearest_llm": excerpt(nearest, 200), "cosine": round(s, 3)})
            if s >= sim_thresh: high_tokens += len(fseg.split())
    # Fallback logic for tfidf or difflib could be added here if needed
    
    mean_sim = 0.0 if not rows else round(sum(r["cosine"] for r in rows) / len(rows), 3)
    high_share = round(high_tokens / max(1, total_tokens), 3)
    return {"backend": SIM_BACKEND, "mean": mean_sim, "high_share": high_share, "rows": rows[:30]}


# ---------- ACADEMIC BACKEND ----------
def render_academic_dashboard():
    st.title("🎓 Academic Dashboard")

    @st.cache_data(ttl=300) # Cache for 5 minutes
    def get_all_student_data():
        try:
            drafts = pd.DataFrame(DRAFTS_WS.get_all_records())
            events = pd.DataFrame(EVENTS_WS.get_all_records())
            return drafts, events
        except Exception as e:
            st.error(f"Could not fetch data from Google Sheets: {e}")
            return pd.DataFrame(), pd.DataFrame()

    drafts_df, events_df = get_all_student_data()
    
    if drafts_df.empty and events_df.empty:
        st.warning("No student data has been recorded yet.")
        st.stop()
        
    all_student_ids = pd.concat([drafts_df['user_id'], events_df['user_id']]).dropna().unique()
    all_student_ids = sorted([str(sid) for sid in all_student_ids if str(sid).strip() and sid != "Academic"])
    
    selected_student = st.selectbox("Select a Student ID to Review", all_student_ids, index=None, placeholder="Search for a student...")
    
    if not selected_student:
        st.info("Please select a student ID from the list to begin.")
        st.stop()
        
    st.header(f"Reviewing: {selected_student}")
    
    student_drafts = drafts_df[drafts_df['user_id'] == selected_student]
    student_events = events_df[events_df['user_id'] == selected_student]
    
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Latest Draft")
        if not student_drafts.empty:
            latest_draft = student_drafts.sort_values('last_updated', ascending=False).iloc[0]
            st.markdown(f"**Last Saved:** {latest_draft['last_updated']}")
            with st.container(height=400, border=True):
                st_html(latest_draft['draft_html'], height=380, scrolling=True)
            st.session_state.latest_draft_text = latest_draft['draft_text']
        else:
            st.info("No saved drafts for this student.")
            st.session_state.latest_draft_text = ""
            
    with col2:
        st.subheader("Chat History")
        chat_history = student_events[student_events['event_type'].str.contains('chat', na=False)].sort_values('timestamp')
        bubbles = []
        if not chat_history.empty:
            for _, row in chat_history.iterrows():
                role, content = (row['event_type'], row['prompt']) if row['event_type'] == 'chat_user' else (row['event_type'], row['response'])
                css = 'chat-user' if role == 'chat_user' else 'chat-assistant'
                bubbles.append(f'<div class="chat-bubble {css}">{md_to_html(content)}</div>')
            st_html(f'<div class="chat-box" style="height:425px;">{"".join(bubbles)}</div>', height=450)
        else:
            st.info("No chat history for this student.")

    st.subheader("Similarity Analysis")
    llm_outputs = student_events[student_events['event_type'] == 'chat_llm']['response'].tolist()
    
    if st.button("Run Similarity Report on Latest Draft", use_container_width=True):
        draft_text = st.session_state.get('latest_draft_text', '')
        if draft_text and llm_outputs:
            with st.spinner("Calculating similarity..."):
                report = compute_similarity_report(draft_text, llm_outputs, SIM_THRESHOLD)
            st.success(f"Report Generated (using {report['backend']})")
            m1, m2 = st.columns(2)
            m1.metric("Mean Similarity", f"{report['mean']:.3f}")
            m2.metric(f"Content >{SIM_THRESHOLD*100}% Similar", f"{report['high_share']*100:.1f}%")
            
            with st.expander("Show Detailed Report"):
                st.dataframe(report['rows'], use_container_width=True)
        else:
            st.warning("Cannot run report. Student needs a saved draft and at least one LLM interaction.")

# ---------- STUDENT VIEW ----------
def render_student_view():
    # --- Session State Init ---
    st.session_state.setdefault("assignment_id", ASSIGNMENT_DEFAULT)
    st.session_state.setdefault("chat", [])
    st.session_state.setdefault("llm_outputs", [])
    st.session_state.setdefault("draft_html", "")
    st.session_state.setdefault("report", None)
    st.session_state.setdefault("last_saved_at", None)
    st.session_state.setdefault("last_autosave_at", None)
    st.session_state.setdefault("last_saved_html", "")
    st.session_state.setdefault("pending_prompt", None)

    # --- Student-specific Helpers ---
    def append_row_safe(ws, row):
        try: ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e: st.warning(f"Append failed: {e}")

    def log_event(event_type: str, prompt: str, response: str):
        append_row_safe(EVENTS_WS, [datetime.datetime.now().isoformat(), st.session_state.user_id, st.session_state.assignment_id, len(st.session_state.chat), event_type, excerpt(prompt, 500), excerpt(response, 1000)])
    
    def ask_llm(prompt_text: str):
        chunks = []
        try:
            for ch in LLM.generate_content([prompt_text], stream=True):
                if getattr(ch, "text", None): chunks.append(ch.text)
        except Exception as e: chunks.append(f"Error: {e}")
        return "".join(chunks)

    def save_progress(silent=False):
        draft_text = html_to_text(st.session_state.draft_html)
        append_row_safe(DRAFTS_WS, [st.session_state.user_id, st.session_state.assignment_id, st.session_state.draft_html, draft_text, datetime.datetime.now().isoformat()])
        st.session_state.last_saved_at = datetime.datetime.now()
        st.session_state.last_saved_html = st.session_state.draft_html
        if not silent: st.toast("Draft saved")

    def load_progress():
        try:
            records = DRAFTS_WS.get_all_records()
            for r in reversed(records):
                if str(r.get("user_id","")).strip().upper() == st.session_state.user_id.strip().upper():
                    return r.get("draft_html") or ""
        except Exception: return ""
        return ""
    
    def maybe_autosave():
        now = time.time()
        if (st.session_state.draft_html != st.session_state.last_saved_html) and \
           (now - (st.session_state.last_autosave_at or 0) >= AUTO_SAVE_SECONDS):
            save_progress(silent=True)
            st.session_state.last_autosave_at = now

    # --- Landing Page ---
    if st.session_state.get("show_landing_page", False):
        st.markdown('<div class="landing-container">', unsafe_allow_html=True)
        st.title("Welcome to the LLM Coursework Helper")
        st.markdown("This tool is designed to support you through your coursework writing process.")

        c1, c2 = st.columns(2)
        with c1:
            st.subheader("💬 AI Assistant & Drafting Space")
            st.markdown("Use the chat to brainstorm and the editor to write. Your work is saved automatically.")
            st.subheader("🔍 Evidence Trail")
            st.markdown("Every interaction with the AI is logged, creating a record of your process.")
        with c2:
            st.subheader("📊 Similarity Check")
            st.markdown("Run a report to see how similar your text is to the AI's suggestions.")
            st.subheader("✅ Academic Oversight")
            st.markdown("**Important:** This tool promotes academic integrity. Your interaction logs and drafts may be reviewed by your instructor as part of your assessment.")
        
        st.markdown("---")
        if st.button("Get Started", type="primary", use_container_width=True):
            st.session_state.show_landing_page = False
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        st.stop()

    # --- Main Student App UI ---
    st.markdown(f'<div class="header-bar"><div class="status-chip">User: {st.session_state.user_id}</div><div class="status-chip">Similarity: {SIM_BACKEND}</div><div class="small-muted">Last saved: {st.session_state.last_saved_at.strftime("%H:%M:%S") if st.session_state.last_saved_at else "—"}</div></div>', unsafe_allow_html=True)
    
    t1, t2, t3 = st.columns([1.2, 0.9, 0.8])
    with t1: st.session_state.assignment_id = st.text_input("Assignment ID", value=st.session_state.assignment_id)
    with t2:
        if st.button("🔄 Load Last Draft"):
            html = load_progress()
            if html: st.session_state.draft_html = html; st.success("Loaded last saved draft."); st.rerun()
            else: st.warning("No saved draft found.")
    with t3:
        if st.button("🧹 Clear Chat"): st.session_state.chat = []; st.session_state.llm_outputs = []; st.toast("Chat cleared")

    left, right = st.columns([0.5, 0.5], gap="large")
    with left:
        st.subheader("💬 Assistant")
        bubbles = ['<div class="chat-empty">Ask for ideas, critique, or examples.</div>'] if not st.session_state.chat else [f'<div class="chat-bubble {"chat-user" if m["role"] == "user" else "chat-assistant"}">{md_to_html(m.get("text", ""))}</div>' for m in st.session_state.chat]
        st_html(f'<div id="chatbox" class="chat-box">{"".join(bubbles)}</div><script>document.getElementById("chatbox").scrollTop = 99999;</script>', height=450)
        
        with st.form("chat_form", clear_on_submit=True):
            c1, c2 = st.columns([4, 1])
            with c1: prompt = st.text_input("Ask…", "", placeholder="Type and press Send", label_visibility="collapsed")
            with c2: send = st.form_submit_button("Send")
        
        if send and prompt.strip():
            st.session_state.chat.append({"role": "user", "text": prompt})
            log_event("chat_user", prompt, "")
            st.session_state.pending_prompt = prompt
            st.rerun()

    with right:
        st.subheader("📝 Draft")
        st.session_state.draft_html = st_quill(value=st.session_state.draft_html, key="editor", html=True, placeholder="Write your draft here...")
        maybe_autosave()

        st.markdown("<br>", unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("💾 Save Draft"): save_progress()
        with c2:
            if st.button("📊 Run Similarity"):
                plain_text = html_to_text(st.session_state.draft_html)
                if plain_text.strip() and st.session_state.llm_outputs:
                    st.session_state.report = compute_similarity_report(plain_text, st.session_state.llm_outputs, SIM_THRESHOLD)
                    rep = st.session_state.report
                    st.success(f"Mean: {rep['mean']} | High-sim: {rep['high_share']*100:.1f}%")
                    log_event("similarity_run", f"mean={rep['mean']}, high_share={rep['high_share']}", "")
                else: st.warning("Need draft text + at least one LLM response.")
        with c3:
            # The download button logic can be more complex, so it's simplified here
            st.download_button("⬇️ Export Evidence", "Feature coming soon.", disabled=True)

    if st.session_state.pending_prompt:
        with st.spinner("Generating response…"):
            p = st.session_state.pending_prompt
            st.session_state.pending_prompt = None
            reply = ask_llm(p)
            st.session_state.chat.append({"role": "assistant", "text": reply})
            st.session_state.llm_outputs.append(reply)
            log_event("chat_llm", p, reply)
        st.rerun()


# ---------- MAIN APP ROUTER ----------
if __name__ == "__main__":
    if st.session_state.get("is_academic"):
        render_academic_dashboard()
    else:
        render_student_view()