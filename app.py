# app.py
import os, io, json, time, datetime, hashlib, random, string, html as _html
import streamlit as st
from streamlit.components.v1 import html as st_html
from streamlit_quill import st_quill
import pandas as pd

# ======================
# Optional / external libs
# ======================
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
    import docx  # python-docx (used by academic export later if you enable it)
    DOCX_OK = True
except Exception:
    DOCX_OK = False

# Markdown renderer for nicer assistant messages (optional; has safe fallback)
try:
    import markdown as _md
except Exception:
    _md = None

# ======================
# Similarity backend
# ======================
SIM_BACKEND = "none"
try:
    from sentence_transformers import SentenceTransformer, util as sbert_util

    @st.cache_resource
    def load_sbert_model():
        return SentenceTransformer("all-MiniLM-L6-v2")

    _sbert_model = load_sbert_model()
    SIM_BACKEND = "sbert"
except Exception:
    # (Fallbacks could be added here if needed)
    pass

# ======================
# Helpers
# ======================
try:
    from bs4 import BeautifulSoup
    def html_to_text(html: str) -> str:
        return BeautifulSoup(html or "", "html.parser").get_text("\n")
except Exception:
    def html_to_text(html: str) -> str:
        return (html or "").replace("<br>", "\n").replace("<br/>", "\n")

def _gen_id(n=6) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))

def excerpt(text: str, n=300) -> str:
    t = text or ""
    return t if len(t) <= n else t[:n] + " …"

def md_to_html(text: str) -> str:
    """Render Markdown (tables, lists, code). Falls back to simple HTML if
    'markdown' package isn't available."""
    if not text:
        return ""
    if _md:
        try:
            return _md.markdown(
                text,
                extensions=["fenced_code", "tables", "sane_lists", "codehilite"],
            )
        except Exception:
            pass
    import re, html as _h
    t = _h.escape(text or "")
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    return t.replace("\n", "<br>")

def quill_value_to_html(value, fallback_html=""):
    """Make streamlit-quill return consistent HTML across versions."""
    # Newer versions can return dict with 'html'
    if isinstance(value, dict):
        if value.get("html"):
            return value["html"]
        # Try to reconstruct text from delta if no html available
        delta = value.get("delta") or value.get("ops") or {}
        ops = delta.get("ops") if isinstance(delta, dict) else delta
        try:
            text = "".join(op.get("insert", "") for op in ops) if isinstance(ops, list) else ""
        except Exception:
            text = ""
        return "<p>" + text.replace("\n", "</p><p>") + "</p>" if text else (fallback_html or "")
    # Some versions return the HTML string directly
    if isinstance(value, str):
        return value
    return fallback_html or ""

# ======================
# Page config + CSS
# ======================
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

/* Chat */
.chat-box { height: 420px; overflow-y:auto; border:1px solid #dcdfe6; border-radius:10px; background:#fff; padding:.5rem; }
.chat-empty{ border:1px dashed #e6e9f2; background:#fbfbfb; color:#708090; padding:.6rem .8rem; border-radius:10px; }
.chat-bubble { border-radius:12px; padding:.7rem .9rem; margin:.45rem .2rem; border:1px solid #eee; line-height:1.55; font-size:0.95rem; }
.chat-user      { background:#eef7ff; }
.chat-assistant { background:#f6f6f6; }
.chat-bubble p { margin:.35rem 0; }
.chat-bubble ul, .chat-bubble ol { margin:.35rem 0 .35rem 1.25rem; }
.chat-bubble table { border-collapse:collapse; width:100%; margin:.35rem 0; }
.chat-bubble a { color:#2563eb; text-decoration:none; }
.chat-bubble a:hover { text-decoration:underline; }
.chat-bubble code { background:#f3f4f6; padding:.05rem .25rem; border-radius:4px; }
.chat-bubble pre { background:#111827; color:#f9fafb; padding:.7rem .9rem; border-radius:10px; overflow:auto; font-size:.9rem; }

/* Landing card */
.landing-container { max-width: 800px; margin: 2rem auto; padding: 2rem; background-color: #fcfdff; border: 1px solid #e6e9f2; border-radius: 10px; }
.landing-container h1 { font-size: 2.0rem; color: #111; }
.landing-container .stButton button { height: 3rem; font-size: 1.1rem; }
</style>
""", unsafe_allow_html=True)

# ======================
# Auth / Global state
# ======================
APP_PASSCODE       = os.getenv("APP_PASSCODE")       or st.secrets.get("env", {}).get("APP_PASSCODE")
ACADEMIC_PASSCODE  = os.getenv("ACADEMIC_PASSCODE")  or st.secrets.get("env", {}).get("ACADEMIC_PASSCODE")
SPREADSHEET_KEY    = os.getenv("SPREADSHEET_KEY", "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ")
ASSIGNMENT_DEFAULT = os.getenv("ASSIGNMENT_ID", "GENERIC")
SIM_THRESHOLD      = float(os.getenv("SIM_THRESHOLD", "0.85"))
AUTO_SAVE_SECONDS  = int(os.getenv("AUTO_SAVE_SECONDS", "60"))

st.session_state.setdefault("__auth_ok", False)
st.session_state.setdefault("user_id", None)
st.session_state.setdefault("is_academic", False)
st.session_state.setdefault("show_landing_page", True)

# ======================
# External clients
# ======================
@st.cache_resource
def get_gspread_client():
    sa_info_obj = os.getenv("GCP_SERVICE_ACCOUNT_JSON") or st.secrets.get("gcp_service_account")
    if not sa_info_obj:
        st.error("GCP Service Account credentials not found in secrets.")
        st.stop()
    if isinstance(sa_info_obj, str):
        try:
            sa_info = json.loads(sa_info_obj)
        except json.JSONDecodeError:
            st.error("Invalid GCP Service Account JSON string.")
            st.stop()
    elif isinstance(sa_info_obj, dict):
        sa_info = sa_info_obj
    else:
        st.error(f"Unexpected type for GCP credentials: {type(sa_info_obj)}")
        st.stop()

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_KEY)

@st.cache_resource
def get_llm_client():
    gemini_key = os.getenv("GEMINI_API_KEY") or st.secrets.get("google_api", {}).get("gemini_api_key")
    if not gemini_key:
        st.error("Gemini API key not found in secrets.")
        st.stop()
    genai.configure(api_key=gemini_key)
    return genai.GenerativeModel("gemini-1.5-flash")

# Initialize now (fail fast if misconfigured)
if gspread is None or Credentials is None:
    st.error("Google Sheets client not available.")
    st.stop()
if genai is None:
    st.error("Gemini client library not available.")
    st.stop()

sh  = get_gspread_client()
LLM = get_llm_client()

@st.cache_resource
def _get_or_create_ws(title, headers):
    try:
        return sh.worksheet(title)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws

EVENTS_WS = _get_or_create_ws("events", ["timestamp","user_id","assignment_id","turn_count","event_type","prompt","response"])
DRAFTS_WS = _get_or_create_ws("drafts", ["user_id","assignment_id","draft_html","draft_text","last_updated"])

# ======================
# Login flow
# ======================
if not st.session_state["__auth_ok"]:
    st.title("LLM Coursework Helper Login")

    @st.cache_data(ttl=60)
    def get_all_student_ids():
        try:
            records = DRAFTS_WS.get_all_records()
            if not records:
                return set()
            df = pd.DataFrame(records)
            return set(df['user_id'].astype(str).unique())
        except Exception:
            return set()

    user_input = st.text_input(
        "Enter your ID or a Passcode",
        placeholder="Student ID, Student Passcode, or Academic Passcode",
    )

    if st.button("Login", use_container_width=True):
        input_cleaned = (user_input or "").strip().upper()
        if input_cleaned == (ACADEMIC_PASSCODE or "").upper():
            st.session_state.update({"__auth_ok": True, "is_academic": True, "user_id": "Academic"})
            st.success("Logged in as Academic.")
            st.rerun()
        elif input_cleaned == (APP_PASSCODE or "").upper():
            new_id = _gen_id()
            st.session_state.update({"__auth_ok": True, "is_academic": False, "user_id": new_id, "show_landing_page": True})
            st.success(f"Welcome! Your new Student ID is **{new_id}**")
            st.info("Copy this ID and use it next time to resume.")
            st.rerun()
        elif input_cleaned in get_all_student_ids():
            st.session_state.update({"__auth_ok": True, "is_academic": False, "user_id": input_cleaned, "show_landing_page": False})
            st.success(f"Welcome back, {input_cleaned}!")
            st.rerun()
        else:
            st.error("Invalid ID or Passcode. Please check and try again.")
    st.stop()

# ======================
# Shared computations
# ======================
def compute_similarity_report(final_text, llm_texts, sim_thresh=SIM_THRESHOLD):
    finals   = [p.strip() for p in (final_text or "").split("\n") if p.strip()]
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
            if s >= sim_thresh:
                high_tokens += len(fseg.split())

    mean_sim   = 0.0 if not rows else round(sum(r["cosine"] for r in rows) / len(rows), 3)
    high_share = round(high_tokens / max(1, total_tokens), 3)
    return {"backend": SIM_BACKEND, "mean": mean_sim, "high_share": high_share, "rows": rows[:30]}

# ======================
# Academic dashboard
# ======================
def render_academic_dashboard():
    st.title("🎓 Academic Dashboard")

    @st.cache_data(ttl=300)
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

    selected_student = st.selectbox(
        "Select a Student ID to Review",
        all_student_ids, index=None, placeholder="Search for a student..."
    )
    if not selected_student:
        st.info("Select a student ID to begin.")
        st.stop()

    st.header(f"Reviewing: {selected_student}")

    student_drafts = drafts_df[drafts_df['user_id'] == selected_student] if not drafts_df.empty else pd.DataFrame()
    student_events = events_df[events_df['user_id'] == selected_student] if not events_df.empty else pd.DataFrame()

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Latest Draft")
        if not student_drafts.empty:
            latest_draft = student_drafts.sort_values('last_updated', ascending=False).iloc[0]
            st.markdown(f"**Last Saved:** {latest_draft['last_updated']}")
            # Use st_html directly (some Streamlit versions don't support container(height=...))
            st_html(latest_draft['draft_html'], height=380, scrolling=True)
            st.session_state.latest_draft_text = latest_draft['draft_text']
        else:
            st.info("No saved drafts for this student.")
            st.session_state.latest_draft_text = ""

    with col2:
        st.subheader("Chat History")
        chat_history = student_events[student_events['event_type'].str.contains('chat', na=False)].sort_values('timestamp') if not student_events.empty else pd.DataFrame()
        bubbles = []
        if not chat_history.empty:
            for _, row in chat_history.iterrows():
                if row['event_type'] == 'chat_user':
                    css = 'chat-user'; content = row['prompt']
                else:
                    css = 'chat-assistant'; content = row['response']
                bubbles.append(f'<div class="chat-bubble {css}">{md_to_html(content)}</div>')
            st_html(f'<div class="chat-box" style="height:425px;">{"".join(bubbles)}</div>', height=450)
        else:
            st.info("No chat history for this student.")

    st.subheader("Similarity Analysis")
    llm_outputs = student_events[student_events['event_type'] == 'chat_llm']['response'].tolist() if not student_events.empty else []

    if st.button("Run Similarity Report on Latest Draft", use_container_width=True):
        draft_text = st.session_state.get('latest_draft_text', '')
        if draft_text and llm_outputs:
            with st.spinner("Calculating similarity..."):
                report = compute_similarity_report(draft_text, llm_outputs, SIM_THRESHOLD)
            st.success(f"Report Generated (using {report['backend']})")
            m1, m2 = st.columns(2)
            m1.metric("Mean Similarity", f"{report['mean']:.3f}")
            m2.metric(f"Content ≥{int(SIM_THRESHOLD*100)}% Similar", f"{report['high_share']*100:.1f}%")
            with st.expander("Show Detailed Report"):
                st.dataframe(report['rows'], use_container_width=True)
        else:
            st.warning("Cannot run report. Student needs a saved draft and at least one LLM interaction.")

# ======================
# Student view
# ======================
def render_student_view():
    # Session defaults
    st.session_state.setdefault("assignment_id", ASSIGNMENT_DEFAULT)
    st.session_state.setdefault("chat", [])
    st.session_state.setdefault("llm_outputs", [])
    st.session_state.setdefault("draft_html", "")
    st.session_state.setdefault("report", None)
    st.session_state.setdefault("last_saved_at", None)
    st.session_state.setdefault("last_autosave_at", None)
    st.session_state.setdefault("last_saved_html", "")
    st.session_state.setdefault("pending_prompt", None)

    # Sheet helpers
    def append_row_safe(ws, row):
        try:
            ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e:
            st.warning(f"Append failed: {e}")

    def log_event(event_type: str, prompt: str, response: str):
        append_row_safe(
            EVENTS_WS,
            [
                datetime.datetime.now().isoformat(),
                st.session_state.user_id,
                st.session_state.assignment_id,
                len(st.session_state.chat),
                event_type,
                excerpt(prompt, 500),
                excerpt(response, 1000),
            ],
        )

    def ask_llm(prompt_text: str) -> str:
        # Stream and stitch output to keep latency good
        try:
            return "".join(
                ch.text for ch in LLM.generate_content([prompt_text], stream=True)
                if getattr(ch, "text", None)
            )
        except Exception as e:
            return f"Error: {e}"

    def save_progress(silent=False):
        draft_text = html_to_text(st.session_state.draft_html)
        append_row_safe(
            DRAFTS_WS,
            [
                st.session_state.user_id,
                st.session_state.assignment_id,
                st.session_state.draft_html,
                draft_text,
                datetime.datetime.now().isoformat(),
            ],
        )
        st.session_state.update(
            {"last_saved_at": datetime.datetime.now(), "last_saved_html": st.session_state.draft_html}
        )
        if not silent:
            st.toast("Draft saved")

    def load_progress():
        try:
            records = DRAFTS_WS.get_all_records()
            for r in reversed(records):
                if str(r.get("user_id", "")).strip().upper() == st.session_state.user_id.strip().upper():
                    return r.get("draft_html") or ""
        except Exception:
            return ""
        return ""

    def maybe_autosave():
        now = time.time()
        if (
            (st.session_state.draft_html != st.session_state.last_saved_html)
            and (now - (st.session_state.last_autosave_at or 0) >= AUTO_SAVE_SECONDS)
        ):
            save_progress(silent=True)
            st.session_state.last_autosave_at = now

    # Optional landing
    if st.session_state.get("show_landing_page", False):
        st.markdown('<div class="landing-container">', unsafe_allow_html=True)
        st.title("Welcome to the LLM Coursework Helper")
        st.markdown("This tool supports your writing process and records an evidence trail.")
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("💬 Assistant & Draft"); st.markdown("Brainstorm in chat, write in the editor. Autosave is on.")
            st.subheader("🔍 Evidence"); st.markdown("Your interactions are logged for integrity & reflection.")
        with c2:
            st.subheader("📊 Similarity"); st.markdown("See how close your final text is to the AI’s examples.")
            st.subheader("✅ Oversight"); st.markdown("Your instructor may review logs/drafts as part of assessment.")
        st.markdown("---")
        if st.button("Get Started", type="primary", use_container_width=True):
            st.session_state.show_landing_page = False
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        st.stop()

    # Header bar
    st.markdown(
        f'<div class="header-bar">'
        f'<div class="status-chip">User ID: {st.session_state.user_id}</div>'
        f'<div class="status-chip">Similarity: {SIM_BACKEND}</div>'
        f'<div class="small-muted">Last saved: {st.session_state.last_saved_at.strftime("%H:%M:%S") if st.session_state.last_saved_at else "—"}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Top controls
    t1, t2, t3 = st.columns([1.2, 0.9, 0.8])
    with t1:
        st.session_state.assignment_id = st.text_input(
            "Assignment ID", value=st.session_state.assignment_id, label_visibility="collapsed"
        )
    with t2:
        if st.button("🔄 Load Last Draft"):
            html = load_progress()
            if html:
                st.session_state.draft_html = html
                st.success("Loaded last saved draft.")
                st.rerun()
            else:
                st.warning("No saved draft found.")
    with t3:
        if st.button("🧹 Clear Chat"):
            st.session_state.chat = []
            st.session_state.llm_outputs = []
            st.toast("Chat cleared")

    # Layout
    left, right = st.columns([0.5, 0.5], gap="large")

    # LEFT — Assistant
    with left:
        st.subheader("💬 Assistant")

        bubbles = []
        if not st.session_state.chat:
            bubbles.append('<div class="chat-empty">Ask for ideas, critique, or examples.</div>')
        else:
            for m in st.session_state.chat:
                css = "chat-user" if m.get("role") == "user" else "chat-assistant"
                content = md_to_html(m.get("text", "")) if m.get("role") != "user" else _html.escape(m.get("text", "")).replace("\n", "<br>")
                bubbles.append(f'<div class="chat-bubble {css}">{content}</div>')

        st_html(
            f'<div id="chatbox" class="chat-box">{"".join(bubbles)}</div>'
            '<script>const b=document.getElementById("chatbox"); if(b){b.scrollTop=b.scrollHeight;}</script>',
            height=450,
        )

        with st.form("chat_form", clear_on_submit=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                prompt = st.text_input("Ask…", placeholder="Type and press Send", label_visibility="collapsed")
            with c2:
                send = st.form_submit_button("Send")

        if send and (prompt or "").strip():
            # Show the user's prompt immediately
            st.session_state.chat.append({"role": "user", "text": prompt})
            log_event("chat_user", prompt, "")
            st.session_state.pending_prompt = prompt
            st.rerun()

    # RIGHT — Draft
    with right:
        st.subheader("📝 Draft")

        # Robust Quill usage across versions
        try:
            quill_raw = st_quill(value=st.session_state.draft_html, key="editor", html=True, placeholder="Write your draft here...")
        except TypeError:
            # Older versions don’t accept html=True
            quill_raw = st_quill(value=st.session_state.draft_html, key="editor", placeholder="Write your draft here...")

        st.session_state.draft_html = quill_value_to_html(quill_raw, st.session_state.draft_html)
        maybe_autosave()

        st.markdown("<br>", unsafe_allow_html=True)

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("💾 Save Draft"):
                save_progress()
        with c2:
            if st.button("📊 Run Similarity"):
                plain_text = html_to_text(st.session_state.draft_html)
                if plain_text.strip() and st.session_state.llm_outputs:
                    st.session_state.report = compute_similarity_report(plain_text, st.session_state.llm_outputs, SIM_THRESHOLD)
                    rep = st.session_state.report
                    st.success(f"Mean: {rep['mean']} | High-sim: {rep['high_share']*100:.1f}%")
                    log_event("similarity_run", f"mean={rep['mean']}, high_share={rep['high_share']}", "")
                else:
                    st.warning("Need draft text + at least one LLM response.")
        with c3:
            st.download_button("⬇️ Export Evidence", "Feature coming soon.", disabled=True)

    # After UI renders, generate response if needed (so user sees their prompt while waiting)
    if st.session_state.pending_prompt:
        with st.spinner("Generating response…"):
            p = st.session_state.pending_prompt
            st.session_state.pending_prompt = None
            reply = ask_llm(p)
            st.session_state.chat.append({"role": "assistant", "text": reply})
            st.session_state.llm_outputs.append(reply)
            log_event("chat_llm", p, reply)
        st.rerun()

# ======================
# Rout
# ======================
if st.session_state.get("is_academic"):
    render_academic_dashboard()
else:
    render_student_view()
