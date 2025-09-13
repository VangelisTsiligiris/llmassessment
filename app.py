import os, io, json, time, datetime, hashlib, random, string, html as _html
import streamlit as st
from streamlit_quill import st_quill  # rich editor

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

# Similarity backends (auto-fallback)
SIM_BACKEND = "none"
try:
    from sentence_transformers import SentenceTransformer, util as sbert_util
    _sbert_model = SentenceTransformer("all-MiniLM-L6-v2")
    SIM_BACKEND = "sbert"
except Exception:
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        SIM_BACKEND = "tfidf"
    except Exception:
        from difflib import SequenceMatcher
        SIM_BACKEND = "difflib"

# HTML → text for Quill content
try:
    from bs4 import BeautifulSoup
    def html_to_text(html: str) -> str:
        return BeautifulSoup(html or "", "html.parser").get_text("\n")
except Exception:
    def html_to_text(html: str) -> str:
        return (html or "").replace("<br>", "\n").replace("<br/>", "\n")

# ---------- Page config ----------
st.set_page_config(
    page_title="LLM Coursework Helper",
    layout="wide",
    menu_items={"Get help": None, "Report a bug": None, "About": None},
)

# ---------- CSS ----------
st.markdown("""
<style>
.block-container {padding-top: 1rem; padding-bottom: 1rem;}

/* Layout columns */
.left-col, .right-col {display:flex; flex-direction:column; height:75vh;}

/* Assistant */
.chat-scroll {
  flex:1; min-height:0; overflow-y:auto;
  padding:.5rem; border:1px solid #eee; border-radius:10px; background:#fff;
}
.chat-bubble {border-radius:12px; padding:.6rem .8rem; margin:.4rem .2rem; border:1px solid #eee;}
.chat-user {background:#eef7ff;}
.chat-assistant {background:#f6f6f6;}
.chat-form {position: sticky; bottom: 0; background: #fafafa; padding:.5rem; border-top:1px solid #ddd;}

/* Draft editor */
.editor-box {
  flex:1; min-height:0; overflow-y:auto;
  border:1px solid #eee; border-radius:10px;
}
.ql-container.ql-snow {min-height:100%; border:none;}
.ql-toolbar.ql-snow {border:none; border-bottom:1px solid #ddd;}
</style>
""", unsafe_allow_html=True)

# ---------- Pilot gate with User ID ----------
def _gen_id(n=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))

APP_PASSCODE = os.getenv("APP_PASSCODE") or st.secrets.get("env", {}).get("APP_PASSCODE")
st.session_state.setdefault("__auth_ok", False)
st.session_state.setdefault("user_id", None)

if APP_PASSCODE and not st.session_state["__auth_ok"]:
    st.title("Pilot access")
    colA, colB = st.columns([2, 2])
    with colA:
        passcode = st.text_input("Enter passcode", type="password")
    with colB:
        sid = st.text_input("Your User ID (keep this to resume later)", value=st.session_state.get("user_id") or "")
        if st.button("Generate new ID"):
            sid = _gen_id()
            st.session_state["user_id"] = sid
            st.info(f"Your new ID is **{sid}** — copy it somewhere safe.")
            st.rerun()
    if st.button("Enter"):
        if passcode == APP_PASSCODE:
            if not sid.strip():
                sid = _gen_id()
            st.session_state["user_id"] = sid.strip().upper()
            st.session_state["__auth_ok"] = True
            st.success(f"Signed in as **{st.session_state['user_id']}**")
            st.rerun()
        else:
            st.error("Wrong passcode.")
    st.stop()

# ---------- Environment config ----------
SPREADSHEET_KEY    = os.getenv("SPREADSHEET_KEY", "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ")
ASSIGNMENT_DEFAULT = os.getenv("ASSIGNMENT_ID", "GENERIC")
SIM_THRESHOLD      = float(os.getenv("SIM_THRESHOLD", "0.85"))
AUTO_SAVE_SECONDS  = int(os.getenv("AUTO_SAVE_SECONDS", "60"))

# ---------- Helpers ----------
def excerpt(text, n=300):
    t = text or ""
    return t if len(t) <= n else t[:n] + " …"

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def segment_paragraphs(text: str):
    if not text:
        return []
    return [p.strip() for p in text.split("\n") if p.strip()]

def word_count(t: str) -> int:
    return len((t or "").split())

def char_count(t: str) -> int:
    return len(t or "")

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
DRAFTS_WS = _get_or_create_ws("drafts", ["user_id","assignment_id","draft_html","draft_text","last_updated"])

def append_row_safe(ws, row):
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        st.warning(f"Append failed: {e}")

# ---------- Session ----------
if not st.session_state.get("user_id"):
    st.session_state["user_id"] = _gen_id()
if "assignment_id" not in st.session_state:
    st.session_state.assignment_id = ASSIGNMENT_DEFAULT
if "chat" not in st.session_state:
    st.session_state.chat = []
if "llm_outputs" not in st.session_state:
    st.session_state.llm_outputs = []
if "draft_html" not in st.session_state:
    st.session_state.draft_html = ""
if "report" not in st.session_state:
    st.session_state.report = None
if "last_saved_at" not in st.session_state:
    st.session_state.last_saved_at = None
if "last_autosave_at" not in st.session_state:
    st.session_state.last_autosave_at = None
if "last_saved_html" not in st.session_state:
    st.session_state.last_saved_html = ""

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

def save_progress(user_id, assignment_id, draft_html, draft_text, silent=False):
    append_row_safe(DRAFTS_WS, [user_id, assignment_id, draft_html, draft_text, datetime.datetime.now().isoformat()])
    st.session_state.last_saved_at = datetime.datetime.now()
    st.session_state.last_saved_html = draft_html
    if not silent:
        st.toast("Draft saved")

def load_progress(user_id, assignment_id):
    try:
        records = DRAFTS_WS.get_all_records()
        for r in reversed(records):
            if str(r.get("user_id","")).strip().upper() == user_id.strip().upper() and str(r.get("assignment_id","")).strip() == assignment_id.strip():
                return r.get("draft_html") or ""
    except Exception:
        return ""
    return ""

def maybe_autosave(draft_html, draft_text):
    now = time.time()
    last_ts = st.session_state.last_autosave_at or 0
    changed = (draft_html or "") != (st.session_state.last_saved_html or "")
    if changed and (now - last_ts) >= AUTO_SAVE_SECONDS:
        save_progress(st.session_state.user_id, st.session_state.assignment_id, draft_html, draft_text, silent=True)
        st.session_state.last_autosave_at = now

def compute_similarity_report(final_text, llm_texts, sim_thresh=SIM_THRESHOLD):
    finals = segment_paragraphs(final_text)
    llm_segs = [s for t in llm_texts for s in segment_paragraphs(t)]
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
    elif SIM_BACKEND == "tfidf":
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        vectorizer = TfidfVectorizer().fit(finals + llm_segs)
        F = vectorizer.transform(finals); L = vectorizer.transform(llm_segs)
        sims = cosine_similarity(F, L)
        for i, fseg in enumerate(finals):
            j = int(sims[i].argmax()); s = float(sims[i, j]); nearest = llm_segs[j]
            rows.append({"final_seg": excerpt(fseg, 200), "nearest_llm": excerpt(nearest, 200), "cosine": round(s, 3)})
            if s >= sim_thresh: high_tokens += len(fseg.split())
    else:
        from difflib import SequenceMatcher
        def cos_like(a, b): return SequenceMatcher(None, a, b).ratio()
        for fseg in finals:
            best, nearest = 0.0, ""
            for l in llm_segs:
                c = cos_like(fseg, l)
                if c > best: best, nearest = c, l
            rows.append({"final_seg": excerpt(fseg, 200), "nearest_llm": excerpt(nearest, 200), "cosine": round(best, 3)})
            if best >= sim_thresh: high_tokens += len(fseg.split())

    mean_sim = 0.0 if not rows else round(sum(r["cosine"] for r in rows) / len(rows), 3)
    high_share = round(high_tokens / max(1, total_tokens), 3)
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

# ---------- Header bar ----------
with st.container():
    st.markdown(
        f"""
        <div class="header-bar">
          <div><strong>LLM Coursework Helper</strong></div>
          <div class="status-chip">User: {st.session_state.user_id}</div>
          <div class="status-chip">Assignment: {st.session_state.assignment_id}</div>
          <div class="status-chip">Similarity: {SIM_BACKEND}</div>
          <div class="small-muted">Last saved: {st.session_state.last_saved_at.strftime('%H:%M:%S') if st.session_state.last_saved_at else '—'}</div>
        </div>
        """, unsafe_allow_html=True
    )

st.divider()

# ---------- Two-column main ----------
left, right = st.columns([0.5, 0.5], gap="large")

with left:
    st.subheader("💬 Assistant")
    st.markdown('<div class="left-col">', unsafe_allow_html=True)
    # Scrollable chat
    chat_html = ['<div class="chat-scroll">']
    for m in st.session_state.chat:
        css = "chat-user" if m["role"] == "user" else "chat-assistant"
        chat_html.append(f'<div class="chat-bubble {css}">{_html.escape(m["text"])}</div>')
    chat_html.append("</div>")
    st.markdown("".join(chat_html), unsafe_allow_html=True)
    # Sticky prompt form
    st.markdown('<div class="chat-form">', unsafe_allow_html=True)
    with st.form("chat_form", clear_on_submit=True):
        c1, c2 = st.columns([4,1])
        with c1:
            prompt = st.text_input("Ask…", "", placeholder="Type and press Send", label_visibility="collapsed")
        with c2:
            send = st.form_submit_button("Send")
    st.markdown('</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    if send and prompt.strip():
        st.session_state.chat.append({"role": "user", "text": prompt})
        reply, latency = ask_llm(prompt)
        st.session_state.chat.append({"role": "assistant", "text": reply})
        st.session_state.llm_outputs.append(reply)
        log_event("chat_user", prompt, "")
        log_event("chat_llm", prompt, reply)
        st.rerun()

with right:
    st.subheader("📝 Draft")
    st.markdown('<div class="right-col">', unsafe_allow_html=True)
    # Draft editor
    st.session_state.draft_html = st_quill(
        value=st.session_state.draft_html,
        placeholder="Write here…",
        key="editor",
        height=500,
    )
    plain = html_to_text(st.session_state.draft_html)
    k1, k2, k3 = st.columns(3)
    k1.metric("Words", word_count(plain))
    k2.metric("Characters", char_count(plain))
    k3.metric("LLM Responses", len(st.session_state.llm_outputs))
    maybe_autosave(st.session_state.draft_html, plain)
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("💾 Save draft"):
            save_progress(st.session_state.user_id, st.session_state.assignment_id, st.session_state.draft_html, plain, silent=False)
    with c2:
        if st.button("📊 Run similarity"):
            if plain.strip() and st.session_state.llm_outputs:
                report = compute_similarity_report(plain, st.session_state.llm_outputs, SIM_THRESHOLD)
                st.session_state.report = report
                st.success(f"Mean: {report['mean']} | High-sim: {report['high_share']*100:.1f}%")
                log_event("similarity_run", f"mean={report['mean']}, high_share={report['high_share']}", "")
            else:
                st.warning("Need draft text + at least one LLM response first.")
    with c3:
        if st.button("⬇️ Export evidence (DOCX)"):
            try:
                rep = st.session_state.get("report", {"backend": "none", "mean": 0, "high_share": 0, "rows": []})
                data = export_evidence_docx(st.session_state.user_id, st.session_state.assignment_id, st.session_state.chat, st.session_state.draft_html, rep)
                st.download_button("Download DOCX", data=data, file_name=f"evidence_{st.session_state.user_id}.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", use_container_width=True)
                log_event("evidence_export", "", "docx")
            except Exception as e:
                st.error(f"Export failed: {e}")
    st.markdown('</div>', unsafe_allow_html=True)
