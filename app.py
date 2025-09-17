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
    # fallback will be difflib in compute_similarity_report
    pass

# ---------- HTML to Text Helper ----------
try:
    from bs4 import BeautifulSoup

    def html_to_text(html: str) -> str:
        return BeautifulSoup(html or "", "html.parser").get_text("\n")
except Exception:
    def html_to_text(html: str) -> str:
        return (html or "").replace("<br>", "\n").replace("<br/>", "\n")

# ---------- Markdown-lite renderer for assistant bubbles ----------
try:
    import markdown as _md
except Exception:
    _md = None

def md_to_html(text: str) -> str:
    if not text:
        return ""
    if _md:
        try:
            return _md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
        except Exception:
            pass
    # Fallback: escape + **bold** + line breaks
    import re, html as _h
    t = _h.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    return t.replace("\n", "<br>")

# ---------- Page Config + CSS ----------
st.set_page_config(
    page_title="LLM Coursework Helper",
    layout="wide",
    menu_items={"Get help": None, "Report a bug": None, "About": None},
)

st.markdown("""
<style>
:root { --ui-font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, "Noto Sans", "Liberation Sans", sans-serif; }
html, body, [data-testid="stAppViewContainer"] * { font-family: var(--ui-font) !important; }

/* Header chips */
.header-bar {display:flex; gap:.75rem; flex-wrap:wrap; font-size:.95rem; color:#444; margin-bottom:.25rem;}
.status-chip{background:#f5f7fb;border:1px solid #e6e9f2;border-radius:999px;padding:.15rem .6rem}
.small-muted{color:#7a7f8a}

/* Chat box */
.chat-box { height: 420px; overflow-y:auto; border:1px solid #dcdfe6; border-radius:10px; background:#fff; padding:.5rem; }
.chat-empty{ border:1px dashed #e6e9f2; background:#fbfbfb; color:#708090; padding:.6rem .8rem; border-radius:10px; }

/* Bubbles */
.chat-bubble { border-radius:12px; padding:.7rem .9rem; margin:.45rem .2rem; border:1px solid #eee; line-height:1.55; font-size:0.95rem; }
.chat-user      { background:#eef7ff; }
.chat-assistant { background:#f6f6f6; }

/* Markdown readability inside bubbles */
.chat-bubble p { margin:.35rem 0; }
.chat-bubble ul, .chat-bubble ol { margin:.35rem 0 .35rem 1.25rem; }
.chat-bubble table { border-collapse:collapse; width:100%; margin:.35rem 0; }
.chat-bubble table th, .chat-bubble table td { border:1px solid #e5e7eb; padding:.35rem .5rem; }
.chat-bubble a { color:#2563eb; text-decoration:none; }
.chat-bubble a:hover { text-decoration:underline; }
.chat-bubble code { background:#f3f4f6; padding:.05rem .25rem; border-radius:4px; }
.chat-bubble pre { background:#111827; color:#f9fafb; padding:.7rem .9rem; border-radius:10px; overflow:auto; font-size:.9rem; }

/* Landing */
.landing-container { max-width: 800px; margin: 2rem auto; padding: 2rem; background-color: #fcfdff; border: 1px solid #e6e9f2; border-radius: 10px; }
.landing-container h1 { font-size: 2.1rem; color: #111; }
.landing-container .stButton button { height: 3rem; font-size: 1.05rem; }
</style>
""", unsafe_allow_html=True)

# ---------- Globals / Config ----------
def _gen_id(n=6): return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))

APP_PASSCODE       = os.getenv("APP_PASSCODE")       or st.secrets.get("env", {}).get("APP_PASSCODE")
ACADEMIC_PASSCODE  = os.getenv("ACADEMIC_PASSCODE")  or st.secrets.get("env", {}).get("ACADEMIC_PASSCODE")
SPREADSHEET_KEY    = os.getenv("SPREADSHEET_KEY", "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ")
ASSIGNMENT_DEFAULT = os.getenv("ASSIGNMENT_ID", "GENERIC")
SIM_THRESHOLD      = float(os.getenv("SIM_THRESHOLD", "0.85"))
AUTO_SAVE_SECONDS  = int(os.getenv("AUTO_SAVE_SECONDS", "60"))

# ---------- Session ----------
st.session_state.setdefault("__auth_ok", False)
st.session_state.setdefault("user_id", None)
st.session_state.setdefault("is_academic", False)
st.session_state.setdefault("show_landing_page", True)
st.session_state.setdefault("assignment_id", ASSIGNMENT_DEFAULT)
st.session_state.setdefault("chat", [])
st.session_state.setdefault("llm_outputs", [])
st.session_state.setdefault("draft_html", "")
st.session_state.setdefault("report", None)
st.session_state.setdefault("last_saved_at", None)
st.session_state.setdefault("last_autosave_at", None)
st.session_state.setdefault("last_saved_html", "")
st.session_state.setdefault("pending_prompt", None)

# ---------- Helpers ----------
def excerpt(text, n=300):
    t = text or ""
    return t if len(t) <= n else t[:n] + " …"

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

# ---------- Secrets → plain dict (handles AttrDict / dict / JSON str) ----------
def _to_plain(obj):
    if hasattr(obj, "to_dict"):
        return {k: _to_plain(v) for k, v in obj.to_dict().items()}
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj

@st.cache_resource
def get_gspread_client():
    sa_raw = st.secrets.get("gcp_service_account", None)
    if sa_raw is None:
        sa_raw = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    if sa_raw is None:
        st.error("GCP Service Account credentials not found in secrets or env."); st.stop()

    if isinstance(sa_raw, str):
        try:
            sa_info = json.loads(sa_raw)
        except json.JSONDecodeError:
            st.error("GCP_SERVICE_ACCOUNT_JSON must be a valid JSON string."); st.stop()
    else:
        sa_info = _to_plain(sa_raw)

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_KEY)

@st.cache_resource
def get_llm_client():
    gemini_key = os.getenv("GEMINI_API_KEY") or st.secrets.get("google_api", {}).get("gemini_api_key")
    if not gemini_key: st.error("Gemini API key not found in secrets or env."); st.stop()
    genai.configure(api_key=gemini_key)
    return genai.GenerativeModel("gemini-1.5-flash")

# Initialize clients and worksheets
if gspread is None or Credentials is None:
    st.error("Google Sheets libraries are not available."); st.stop()
if genai is None:
    st.error("Gemini client library is not available."); st.stop()

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

def append_row_safe(ws, row):
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        st.warning(f"Append failed: {e}")

# ---------- Login ----------
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
        token = (user_input or "").strip().upper()
        if token and token == (ACADEMIC_PASSCODE or "").upper():
            st.session_state.update({"__auth_ok": True, "is_academic": True, "user_id": "ACADEMIC"})
            st.success("Logged in as Academic."); st.rerun()
        elif token and token == (APP_PASSCODE or "").upper():
            new_id = _gen_id()
            st.session_state.update({"__auth_ok": True, "is_academic": False, "user_id": new_id, "show_landing_page": True})
            st.success(f"Welcome! Your new Student ID is **{new_id}**"); st.info("Copy this ID to resume next time."); st.rerun()
        elif token in get_all_student_ids():
            st.session_state.update({"__auth_ok": True, "is_academic": False, "user_id": token, "show_landing_page": False})
            st.success(f"Welcome back, {token}!"); st.rerun()
        else:
            st.error("Invalid ID or Passcode.")
    st.stop()

# ---------- Core utilities ----------
def log_event(event_type: str, prompt: str, response: str):
    append_row_safe(EVENTS_WS, [
        datetime.datetime.now().isoformat(),
        st.session_state.user_id,
        st.session_state.assignment_id,
        len(st.session_state.chat),
        event_type,
        excerpt(prompt, 500),
        excerpt(response, 1000),
    ])

def ask_llm(prompt_text: str) -> str:
    out = []
    try:
        for ch in LLM.generate_content([prompt_text], stream=True):
            if getattr(ch, "text", None):
                out.append(ch.text)
    except Exception as e:
        out.append(f"Error: {e}")
    return "".join(out)

def save_progress(silent=False):
    draft_text = html_to_text(st.session_state.draft_html)
    append_row_safe(DRAFTS_WS, [
        st.session_state.user_id,
        st.session_state.assignment_id,
        st.session_state.draft_html,
        draft_text,
        datetime.datetime.now().isoformat()
    ])
    st.session_state["last_saved_at"] = datetime.datetime.now()
    st.session_state["last_saved_html"] = st.session_state.draft_html
    if not silent: st.toast("Draft saved")

def load_progress():
    try:
        records = DRAFTS_WS.get_all_records()
        for r in reversed(records):
            if str(r.get("user_id","")).strip().upper() == st.session_state.user_id.strip().upper() and \
               str(r.get("assignment_id","")).strip() == st.session_state.assignment_id.strip():
                return r.get("draft_html") or ""
    except Exception:
        return ""
    return ""

def maybe_autosave():
    now = time.time()
    last = st.session_state.last_autosave_at or 0
    changed = (st.session_state.draft_html or "") != (st.session_state.last_saved_html or "")
    if changed and (now - last) >= AUTO_SAVE_SECONDS:
        save_progress(silent=True)
        st.session_state.last_autosave_at = now

def compute_similarity_report(final_text, llm_texts, sim_thresh=SIM_THRESHOLD):
    finals   = [p.strip() for p in (final_text or "").split("\n") if p.strip()]
    llm_segs = [s.strip() for t in (llm_texts or []) for s in (t or "").split("\n") if s.strip()]
    if not finals or not llm_segs:
        return {"backend": SIM_BACKEND, "mean": 0.0, "high_share": 0.0, "rows": []}

    rows, high_tokens = [], 0
    total_tokens = sum(len(s.split()) for s in finals)

    if SIM_BACKEND == "sbert":
        Ef = _sbert_model.encode(finals, convert_to_tensor=True, normalize_embeddings=True)
        El = _sbert_model.encode(llm_segs, convert_to_tensor=True, normalize_embeddings=True)
        sims = sbert_util.cos_sim(Ef, El).cpu().numpy()
        for i, fseg in enumerate(finals):
            j = int(sims[i].argmax())
            s = float(sims[i, j])
            nearest = llm_segs[j]
            rows.append({"final_seg": excerpt(fseg, 200), "nearest_llm": excerpt(nearest, 200), "cosine": round(s, 3)})
            if s >= sim_thresh: high_tokens += len(fseg.split())
    else:
        # lightweight fallback
        from difflib import SequenceMatcher
        def cos_like(a, b): return SequenceMatcher(None, a, b).ratio()
        for fseg in finals:
            best, nearest = 0.0, ""
            for l in llm_segs:
                c = cos_like(fseg, l)
                if c > best: best, nearest = c, l
            rows.append({"final_seg": excerpt(fseg, 200), "nearest_llm": excerpt(nearest, 200), "cosine": round(best, 3)})
            if best >= sim_thresh: high_tokens += len(fseg.split())

    mean_sim  = 0.0 if not rows else round(sum(r["cosine"] for r in rows) / len(rows), 3)
    high_share= round(high_tokens / max(1, total_tokens), 3)
    return {"backend": SIM_BACKEND, "mean": mean_sim, "high_share": high_share, "rows": rows[:30]}

def export_evidence_docx(user_id, assignment_id, chat, draft_html, report):
    if not DOCX_OK:
        raise RuntimeError("python-docx not installed")
    final_text = html_to_text(draft_html)

    d = docx.Document()
    d.add_heading("Coursework Evidence Pack", 0)
    d.add_paragraph(f"User ID: {user_id}")
    d.add_paragraph(f"Assignment ID: {assignment_id}")
    d.add_paragraph(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    d.add_heading("Chat with LLM", level=1)
    for m in chat:
        role = "Student" if m["role"] == "user" else "LLM"
        d.add_paragraph(f"{role}: {m['text']}")

    d.add_heading("Final Draft (plain text extract)", level=1)
    for para in final_text.split("\n"):
        d.add_paragraph(para)

    d.add_heading("Similarity Report", level=1)
    d.add_paragraph(f"Backend: {report.get('backend','-')}")
    d.add_paragraph(f"Mean similarity: {report.get('mean',0.0)}")
    d.add_paragraph(f"High-sim share: {report.get('high_share',0.0)*100:.1f}%")
    for r in report.get("rows", []):
        d.add_paragraph(f"- Cosine: {r['cosine']} | Final: {r['final_seg']} | LLM: {r['nearest_llm']}")

    buf = io.BytesIO()
    d.save(buf)
    buf.seek(0)
    return buf.read()

# ---------- Academic Dashboard ----------
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
        return

    all_student_ids = pd.concat([drafts_df['user_id'], events_df['user_id']]).dropna().unique()
    all_student_ids = sorted([str(sid) for sid in all_student_ids if str(sid).strip() and sid.upper() != "ACADEMIC"])

    selected_student = st.selectbox("Select a Student ID to Review", all_student_ids, index=None, placeholder="Search for a student...")
    if not selected_student:
        st.info("Please select a student ID to begin.")
        return

    st.header(f"Reviewing: {selected_student}")
    student_drafts = drafts_df[drafts_df['user_id'] == selected_student]
    student_events = events_df[events_df['user_id'] == selected_student]

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Latest Draft")
        if not student_drafts.empty:
            latest = student_drafts.sort_values('last_updated', ascending=False).iloc[0]
            st.markdown(f"**Last Saved:** {latest['last_updated']}")
            st_html(f'<div class="chat-box" style="height:380px;">{latest["draft_html"]}</div>', height=410)
            st.session_state.latest_draft_text = latest['draft_text']
        else:
            st.info("No saved drafts for this student.")
            st.session_state.latest_draft_text = ""

    with col2:
        st.subheader("Chat History")
        chat_history = student_events[student_events['event_type'].str.contains('chat', na=False)].sort_values('timestamp')
        bubbles = []
        if not chat_history.empty:
            for _, row in chat_history.iterrows():
                if row['event_type'] == 'chat_user':
                    bubbles.append(f'<div class="chat-bubble chat-user">{md_to_html(row["prompt"])}</div>')
                elif row['event_type'] == 'chat_llm':
                    bubbles.append(f'<div class="chat-bubble chat-assistant">{md_to_html(row["response"])}</div>')
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
            m2.metric(f"Content ≥{int(SIM_THRESHOLD*100)}% Similar", f"{report['high_share']*100:.1f}%")
            with st.expander("Show Detailed Report"):
                st.dataframe(report['rows'], use_container_width=True)
        else:
            st.warning("Need a saved draft and at least one LLM response.")

# ---------- Student View ----------
def render_student_view():
    # Landing (first time only)
    if st.session_state.get("show_landing_page", False):
        st.markdown('<div class="landing-container">', unsafe_allow_html=True)
        st.title("Welcome to the LLM Coursework Helper")
        st.markdown("Use the AI assistant to brainstorm and the editor to write. Your progress is auto-saved to your institution’s Google Sheet.")
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("💬 Assistant & Draft")
            st.markdown("- Ask for ideas, outlines, or feedback.\n- Keep your own writing voice in the editor.")
            st.subheader("🔍 Evidence Trail")
            st.markdown("- All interactions are logged to promote constructive use.")
        with c2:
            st.subheader("📊 Similarity Check")
            st.markdown("Compare your draft against the assistant outputs to gauge reliance.")
            st.subheader("✅ Academic Oversight")
            st.markdown("Your instructor may review your process logs as part of assessment.")
        st.markdown("---")
        if st.button("Get Started", type="primary", use_container_width=True):
            st.session_state.show_landing_page = False
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    # Header chips
    st.markdown(
        f'<div class="header-bar">'
        f'<div class="status-chip">User ID: {st.session_state.user_id}</div>'
        f'<div class="status-chip">Assignment: {st.session_state.assignment_id}</div>'
        f'<div class="status-chip">Similarity backend: {SIM_BACKEND}</div>'
        f'<div class="small-muted">Last saved: {st.session_state.last_saved_at.strftime("%H:%M:%S") if st.session_state.last_saved_at else "—"}</div>'
        f'</div>',
        unsafe_allow_html=True
    )

    # Toolbar
    t1, t2, t3 = st.columns([1.2, 0.9, 0.8])
    with t1:
        st.session_state.assignment_id = st.text_input("Assignment ID", value=st.session_state.assignment_id, label_visibility="collapsed")
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

    # Two columns
    left, right = st.columns([0.5, 0.5], gap="large")

    # LEFT: Assistant
    with left:
        st.subheader("💬 Assistant")

        if not st.session_state.chat:
            bubbles = ['<div class="chat-empty">Ask for ideas, critique, or examples.</div>']
        else:
            bubbles = []
            for m in st.session_state.chat:
                css = "chat-user" if m["role"] == "user" else "chat-assistant"
                content = _html.escape(m.get("text","")).replace("\n","<br>") if m["role"]=="user" else md_to_html(m.get("text",""))
                bubbles.append(f'<div class="chat-bubble {css}">{content}</div>')

        st_html(
            f'<div id="chatbox" class="chat-box">{"".join(bubbles)}</div>'
            f'<script>var box=document.getElementById("chatbox"); if(box){{box.scrollTop=box.scrollHeight;}}</script>',
            height=450
        )

        # Prompt form (sticky under chat box)
        with st.form("chat_form", clear_on_submit=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                prompt = st.text_input("Ask…", "", placeholder="Type and press Send", label_visibility="collapsed")
            with c2:
                send = st.form_submit_button("Send")

        if send and (prompt or "").strip():
            st.session_state.chat.append({"role": "user", "text": prompt})
            log_event("chat_user", prompt, "")
            st.session_state.pending_prompt = prompt
            st.rerun()

        # Generate reply on next run (after UI renders with the user bubble)
        if st.session_state.pending_prompt:
            with st.spinner("Generating response…"):
                p = st.session_state.pending_prompt
                st.session_state.pending_prompt = None
                reply = ask_llm(p)
                st.session_state.chat.append({"role": "assistant", "text": reply})
                st.session_state.llm_outputs.append(reply)
                log_event("chat_llm", p, reply)
            st.rerun()

    # RIGHT: Draft
    with right:
        st.subheader("📝 Draft")
        st.session_state.draft_html = st_quill(
            value=st.session_state.draft_html,
            key="editor",
            html=True,
            placeholder="Write your draft here..."
        )

        # KPIs
        plain = html_to_text(st.session_state.draft_html)
        k1, k2, k3 = st.columns(3)
        k1.metric("Words", len(plain.split()))
        k2.metric("Characters", len(plain))
        k3.metric("LLM Responses", len(st.session_state.llm_outputs))

        # Auto-save
        maybe_autosave()

        st.markdown("<br>", unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("💾 Save Draft"):
                save_progress()
        with c2:
            if st.button("📊 Run Similarity"):
                if plain.strip() and st.session_state.llm_outputs:
                    report = compute_similarity_report(plain, st.session_state.llm_outputs, SIM_THRESHOLD)
                    st.session_state.report = report
                    st.success(f"Mean: {report['mean']} | High-sim: {report['high_share']*100:.1f}%")
                    log_event("similarity_run", f"mean={report['mean']}, high_share={report['high_share']}", "")
                else:
                    st.warning("Need draft text + at least one LLM response.")
        with c3:
            if st.button("⬇️ Export Evidence (DOCX)"):
                try:
                    rep = st.session_state.get("report", {"backend": SIM_BACKEND, "mean": 0, "high_share": 0, "rows": []})
                    data = export_evidence_docx(
                        st.session_state.user_id,
                        st.session_state.assignment_id,
                        st.session_state.chat,
                        st.session_state.draft_html,
                        rep
                    )
                    st.download_button(
                        "Download DOCX",
                        data=data,
                        file_name=f"evidence_{st.session_state.user_id}.docx",
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        use_container_width=True
                    )
                    log_event("evidence_export", "", "docx")
                except Exception as e:
                    st.error(f"Export failed: {e}")

# ---------- Router ----------
if __name__ == "__main__":
    if st.session_state.get("is_academic"):
        render_academic_dashboard()
    else:
        render_student_view()
