import os, io, json, time, datetime, hashlib, random, string, html as _html
import streamlit as st
from streamlit_quill import st_quill
from streamlit.components.v1 import html as st_html
from streamlit.runtime.secrets import AttrDict  # Import AttrDict for type checking
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

# FIX: Ensure python-docx is checked for the export feature
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
    pass

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
/* Other styles are omitted for brevity but are included in the final code */
.header-bar {display:flex; gap:.75rem; flex-wrap:wrap; font-size:.95rem; color:#444; margin-bottom:.25rem;}
.status-chip{background:#f5f7fb;border:1px solid #e6e9f2;border-radius:999px;padding:.15rem .6rem}
.small-muted{color:#7a7f8a}
.chat-box { height: 420px; overflow-y:auto; border:1px solid #dcdfe6; border-radius:10px; background:#fff; padding:.5rem; }
.chat-empty{ border:1px dashed #e6e9f2; background:#fbfbfb; color:#708090; padding:.6rem .8rem; border-radius:10px; }
.chat-bubble { border-radius:12px; padding:.7rem .9rem; margin:.45rem .2rem; border:1px solid #eee; line-height:1.55; font-size:0.95rem; font-family: var(--ui-font) !important; }
.chat-user      { background:#eef7ff; }
.chat-assistant { background:#f6f6f6; }
</style>
""", unsafe_allow_html=True)

# ---------- GLOBAL SESSION STATE & AUTHENTICATION ----------
def _gen_id(n=6): return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))

APP_PASSCODE = os.getenv("APP_PASSCODE") or st.secrets.get("env", {}).get("APP_PASSCODE")
ACADEMIC_PASSCODE = os.getenv("ACADEMIC_PASSCODE") or st.secrets.get("env", {}).get("ACADEMIC_PASSCODE")

st.session_state.setdefault("__auth_ok", False)
st.session_state.setdefault("user_id", None)
st.session_state.setdefault("is_academic", False)
st.session_state.setdefault("show_landing_page", True)

# ---------- GLOBAL RESOURCES (Clients & Config) ----------
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY", "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ")
ASSIGNMENT_DEFAULT = os.getenv("ASSIGNMENT_ID", "GENERIC")
SIM_THRESHOLD = float(os.getenv("SIM_THRESHOLD", "0.85"))
AUTO_SAVE_SECONDS = int(os.getenv("AUTO_SAVE_SECONDS", "60"))

@st.cache_resource
def get_gspread_client():
    sa_info_obj = os.getenv("GCP_SERVICE_ACCOUNT_JSON") or st.secrets.get("gcp_service_account")
    if not sa_info_obj: st.error("GCP Service Account credentials not found in secrets."); st.stop()

    if isinstance(sa_info_obj, str):
        try: sa_info = json.loads(sa_info_obj)
        except json.JSONDecodeError: st.error("Invalid GCP Service Account JSON string."); st.stop()
    elif isinstance(sa_info_obj, (dict, AttrDict)):
        sa_info = dict(sa_info_obj)
    else:
        st.error(f"Unexpected type for GCP credentials: {type(sa_info_obj)}"); st.stop()

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

# ---------- LOGIN LOGIC ----------
if not st.session_state["__auth_ok"]:
    st.title("LLM Coursework Helper Login")

    @st.cache_data(ttl=60)
    def get_all_student_ids():
        try:
            records = DRAFTS_WS.get_all_records()
            if not records: return set()
            df = pd.DataFrame(records)
            return set(df['user_id'].astype(str).unique())
        except Exception:
            return set()

    user_input = st.text_input("Enter your ID or a Passcode", placeholder="Student ID, Student Passcode, or Academic Passcode")

    if st.button("Login", use_container_width=True):
        input_cleaned = user_input.strip().upper()
        
        if ACADEMIC_PASSCODE and input_cleaned == ACADEMIC_PASSCODE.upper():
            st.session_state.update({"__auth_ok": True, "is_academic": True, "user_id": "Academic"})
            st.rerun()
        elif APP_PASSCODE and input_cleaned == APP_PASSCODE.upper():
            new_id = _gen_id()
            st.session_state.update({"__auth_ok": True, "is_academic": False, "user_id": new_id, "show_landing_page": True})
            st.rerun()
        elif input_cleaned in get_all_student_ids():
            st.session_state.update({"__auth_ok": True, "is_academic": False, "user_id": input_cleaned, "show_landing_page": False})
            st.rerun()
        else:
            st.error("Invalid ID or Passcode. Please check your input and try again.")
    st.stop()


# ---------- GLOBAL HELPER FUNCTIONS ----------
def excerpt(text, n=300):
    t = text or ""
    return t if len(t) <= n else t[:n] + " …"

def md_to_html(text: str) -> str:
    import re, html as _h
    t = _h.escape(text or "")
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    return t.replace("\n", "<br>")

def compute_similarity_report(final_text, llm_texts, sim_thresh=SIM_THRESHOLD):
    # (Implementation is unchanged)
    finals = [p.strip() for p in (final_text or "").split("\n") if p.strip()]
    llm_segs = [s.strip() for t in llm_texts for s in (t or "").split("\n") if s.strip()]
    if not finals or not llm_segs: return {"backend": SIM_BACKEND, "mean": 0.0, "high_share": 0.0, "rows": []}
    rows, high_tokens, total_tokens = [], 0, sum(len(s.split()) for s in finals)
    if SIM_BACKEND == "sbert":
        Ef = _sbert_model.encode(finals, convert_to_tensor=True, normalize_embeddings=True)
        El = _sbert_model.encode(llm_segs, convert_to_tensor=True, normalize_embeddings=True)
        sims = sbert_util.cos_sim(Ef, El).cpu().numpy()
        for i, fseg in enumerate(finals):
            j = int(sims[i].argmax()); s = float(sims[i, j]); nearest = llm_segs[j]
            rows.append({"final_seg": excerpt(fseg, 200), "nearest_llm": excerpt(nearest, 200), "cosine": round(s, 3)})
            if s >= sim_thresh: high_tokens += len(fseg.split())
    mean_sim = 0.0 if not rows else round(sum(r["cosine"] for r in rows) / len(rows), 3)
    high_share = round(high_tokens / max(1, total_tokens), 3)
    return {"backend": SIM_BACKEND, "mean": mean_sim, "high_share": high_share, "rows": rows[:30]}

# FIX: Re-added the export evidence function
def export_evidence_docx(user_id, assignment_id, chat, draft_html, report):
    if not DOCX_OK:
        raise RuntimeError("python-docx library not installed")
    final_text = html_to_text(draft_html)

    d = docx.Document()
    d.add_heading("Coursework Evidence Pack", 0)
    p = d.add_paragraph()
    p.add_run(f"User ID: ").bold = True
    p.add_run(user_id)
    p = d.add_paragraph()
    p.add_run(f"Assignment ID: ").bold = True
    p.add_run(assignment_id)
    p = d.add_paragraph()
    p.add_run(f"Generated: ").bold = True
    p.add_run(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z'))

    d.add_heading("Chat with LLM", level=1)
    for m in chat:
        role = "Student" if m["role"] == "user" else "LLM"
        d.add_paragraph(f"{role}: {m['text']}")

    d.add_heading("Final Draft (plain text extract)", level=1)
    for para in final_text.split("\n"):
        if para.strip(): d.add_paragraph(para)

    d.add_heading("Similarity Report", level=1)
    d.add_paragraph(f"Backend Used: {report.get('backend','-')}")
    d.add_paragraph(f"Mean Similarity (Draft vs. LLM Output): {report.get('mean',0.0)}")
    d.add_paragraph(f"High-Similarity Share (> {SIM_THRESHOLD}): {report.get('high_share',0.0)*100:.1f}%")
    if report.get('rows'):
        d.add_paragraph("\nTop 30 Most Similar Paragraphs:")
        for r in report.get("rows", []):
            d.add_paragraph(f"- Cosine: {r['cosine']} | Draft: '{r['final_seg']}' | LLM: '{r['nearest_llm']}'", style='List Bullet')

    buf = io.BytesIO()
    d.save(buf)
    buf.seek(0)
    return buf.read()


# ---------- ACADEMIC BACKEND ----------
def render_academic_dashboard():
    # (This function is unchanged)
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
        st.warning("No student data has been recorded yet."); st.stop()
    all_student_ids = sorted([str(sid) for sid in pd.concat([drafts_df['user_id'], events_df['user_id']]).dropna().unique() if str(sid).strip() and sid != "Academic"])
    selected_student = st.selectbox("Select a Student ID to Review", all_student_ids, index=None, placeholder="Search for a student...")
    if not selected_student: st.info("Please select a student ID from the list to begin."); st.stop()
    st.header(f"Reviewing: {selected_student}")
    student_drafts = drafts_df[drafts_df['user_id'] == selected_student]
    student_events = events_df[events_df['user_id'] == selected_student]
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Latest Draft")
        if not student_drafts.empty:
            latest_draft = student_drafts.sort_values('last_updated', ascending=False).iloc[0]
            st.markdown(f"**Last Saved:** {latest_draft['last_updated']}")
            with st.container(height=400, border=True): st_html(latest_draft['draft_html'], height=380, scrolling=True)
            st.session_state.latest_draft_text = latest_draft['draft_text']
        else: st.info("No saved drafts for this student."); st.session_state.latest_draft_text = ""
    with col2:
        st.subheader("Chat History")
        chat_history = student_events[student_events['event_type'].str.contains('chat', na=False)].sort_values('timestamp')
        if not chat_history.empty:
            bubbles = [f'<div class="chat-bubble {"chat-user" if row["event_type"] == "chat_user" else "chat-assistant"}">{md_to_html(row["prompt"] if row["event_type"] == "chat_user" else row["response"])}</div>' for _, row in chat_history.iterrows()]
            st_html(f'<div class="chat-box" style="height:425px;">{"".join(bubbles)}</div>', height=450)
        else: st.info("No chat history for this student.")
    st.subheader("Similarity Analysis")
    llm_outputs = student_events[student_events['event_type'] == 'chat_llm']['response'].tolist()
    if st.button("Run Similarity Report on Latest Draft", use_container_width=True):
        if st.session_state.get('latest_draft_text') and llm_outputs:
            report = compute_similarity_report(st.session_state.latest_draft_text, llm_outputs)
            st.success(f"Report Generated"); m1, m2 = st.columns(2); m1.metric("Mean Similarity", f"{report['mean']:.3f}"); m2.metric(f"High-Similarity Content", f"{report['high_share']*100:.1f}%")
            with st.expander("Show Detailed Report"): st.dataframe(report['rows'], use_container_width=True)
        else: st.warning("Cannot run report. Student needs a saved draft and at least one LLM interaction.")


# ---------- STUDENT VIEW ----------
def render_student_view():
    st.session_state.setdefault("assignment_id", ASSIGNMENT_DEFAULT)
    st.session_state.setdefault("chat", [])
    st.session_state.setdefault("llm_outputs", [])
    st.session_state.setdefault("draft_html", "")
    st.session_state.setdefault("report", None)
    st.session_state.setdefault("last_saved_at", None)
    st.session_state.setdefault("last_autosave_at", None)
    st.session_state.setdefault("last_saved_html", "")
    st.session_state.setdefault("pending_prompt", None)

    def append_row_safe(ws, row):
        try: ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e: st.warning(f"Save failed: {e}. Please check GSheet permissions.")

    def log_event(event_type, prompt="", response=""):
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        row = [ts, st.session_state.user_id, st.session_state.assignment_id, len(st.session_state.chat), event_type, excerpt(prompt), excerpt(response)]
        append_row_safe(EVENTS_WS, row)
    
    def ask_llm(prompt_text: str):
        try: return "".join([ch.text for ch in LLM.generate_content([prompt_text], stream=True) if getattr(ch, "text", None)])
        except Exception as e: return f"Error: {e}"

    def save_progress(silent=False):
        draft_text = html_to_text(st.session_state.draft_html)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        row = [st.session_state.user_id, st.session_state.assignment_id, st.session_state.draft_html, draft_text, ts]
        append_row_safe(DRAFTS_WS, row)
        st.session_state.update({"last_saved_at": datetime.datetime.now(datetime.timezone.utc), "last_saved_html": st.session_state.draft_html})
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
        if (st.session_state.draft_html != st.session_state.last_saved_html) and (now - (st.session_state.last_autosave_at or 0) >= AUTO_SAVE_SECONDS):
            save_progress(silent=True)
            st.session_state.last_autosave_at = now

    if st.session_state.get("show_landing_page", False):
        st.markdown('<div class="landing-container">', unsafe_allow_html=True)
        st.title("Welcome to the LLM Coursework Helper")
        st.markdown("This tool is designed to support you through your coursework writing process.")
        c1, c2 = st.columns(2); c1.subheader("💬 AI Assistant & Drafting Space"); c1.markdown("Use the chat to brainstorm and the editor to write. Your work is saved automatically."); c1.subheader("🔍 Evidence Trail"); c1.markdown("Every interaction with the AI is logged, creating a record of your process."); c2.subheader("📊 Similarity Check"); c2.markdown("Run a report to see how similar your text is to the AI's suggestions."); c2.subheader("✅ Academic Oversight"); c2.markdown("**Important:** This tool promotes academic integrity. Your interaction logs and drafts may be reviewed by your instructor as part of your assessment.")
        st.markdown("---")
        if st.button("Get Started", type="primary", use_container_width=True): st.session_state.show_landing_page = False; st.rerun()
        st.markdown('</div>', unsafe_allow_html=True); st.stop()

    st.markdown(f'<div class="header-bar"><div class="status-chip">User ID: {st.session_state.user_id}</div><div class="status-chip">Similarity: {SIM_BACKEND}</div><div class="small-muted">Last saved: {st.session_state.last_saved_at.strftime("%H:%M:%S %Z") if st.session_state.last_saved_at else "—"}</div></div>', unsafe_allow_html=True)
    
    t1, t2, t3 = st.columns([1.2, 0.9, 0.8])
    with t1: st.session_state.assignment_id = st.text_input("Assignment ID", st.session_state.assignment_id, label_visibility="collapsed", placeholder="Enter Assignment ID")
    with t2:
        if st.button("🔄 Load Last Draft"):
            html = load_progress(); st.session_state.draft_html = html; st.success("Loaded last saved draft.") if html else st.warning("No saved draft found."); st.rerun()
    with t3:
        if st.button("🧹 Clear Chat"): st.session_state.chat = []; st.session_state.llm_outputs = []; st.toast("Chat cleared")

    left, right = st.columns([0.5, 0.5], gap="large")
    with left:
        st.subheader("💬 Assistant")
        bubbles = ['<div class="chat-empty">Ask for ideas, critique, or examples.</div>'] if not st.session_state.chat else [f'<div class="chat-bubble {"chat-user" if m["role"] == "user" else "chat-assistant"}">{md_to_html(m.get("text", ""))}</div>' for m in st.session_state.chat]
        st_html(f'<div id="chatbox" class="chat-box">{"".join(bubbles)}</div><script>document.getElementById("chatbox").scrollTop=99999;</script>', height=450)
        with st.form("chat_form", clear_on_submit=True):
            c1, c2 = st.columns([4, 1]); prompt = c1.text_input("Ask…", placeholder="Type and press Send", label_visibility="collapsed"); send = c2.form_submit_button("Send")
        if send and prompt.strip():
            st.session_state.chat.append({"role": "user", "text": prompt}); log_event("chat_user", prompt=prompt)
            st.session_state.pending_prompt = prompt; st.rerun()

    with right:
        st.subheader("📝 Draft")
        st.session_state.draft_html = st_quill(st.session_state.draft_html, key="editor", html=True, placeholder="Write your draft here...")
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
                    st.success(f"Mean: {rep['mean']} | High-sim: {rep['high_share']*100:.1f}%"); log_event("similarity_run", response=f"mean={rep['mean']}")
                else: st.warning("Need draft text + at least one LLM response.")
        
        # FIX: Re-enabled the download button with full functionality
        with c3:
            if DOCX_OK:
                rep = st.session_state.get("report", {"backend":"-", "mean":0, "high_share":0, "rows":[]})
                try:
                    docx_data = export_evidence_docx(st.session_state.user_id, st.session_state.assignment_id, st.session_state.chat, st.session_state.draft_html, rep)
                    st.download_button(
                        label="⬇️ Export Evidence",
                        data=docx_data,
                        file_name=f"evidence_{st.session_state.user_id}_{st.session_state.assignment_id}.docx",
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        use_container_width=True,
                        on_click=log_event,
                        args=("evidence_export",)
                    )
                except Exception as e:
                    st.error(f"Export failed: {e}")
            else:
                st.button("Export Disabled", help="Install python-docx to enable", disabled=True, use_container_width=True)

    if st.session_state.pending_prompt:
        with st.spinner("Generating response…"):
            p = st.session_state.pending_prompt; st.session_state.pending_prompt = None
            reply = ask_llm(p)
            st.session_state.chat.append({"role": "assistant", "text": reply}); st.session_state.llm_outputs.append(reply)
            log_event("chat_llm", prompt=p, response=reply)
        st.rerun()

# ---------- MAIN APP ROUTER ----------
if __name__ == "__main__":
    if st.session_state.get("is_academic"):
        render_academic_dashboard()
    else:
        render_student_view()