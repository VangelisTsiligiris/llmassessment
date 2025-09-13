import os, io, json, time, datetime, hashlib, random, string, html as _html
import streamlit as st
import streamlit.components.v1 as components
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

# ---------- CSS (layout + scrollable chat) ----------
st.markdown("""
<style>
.block-container {padding-top: 1rem; padding-bottom: 1rem;}
.stApp header, [data-testid="stToolbar"], [data-testid="stHeaderActionButtons"] {
  z-index: 1000 !important; position: relative;
}
/* Header bar */
.header-bar {display:flex; align-items:center; gap:.75rem; padding:.6rem 1rem;
  border:1px solid #e6e6e6; border-radius:12px; background:#fafafa;}
.status-chip {display:inline-block; padding:.15rem .5rem; border-radius:999px;
  font-size:.85rem; border:1px solid #ddd; background:white}
.small-muted {color:#666; font-size:.9rem}

/* Chat panel — shorter + bottom padding so last line is visible */
.chat-panel {
  height: 50vh;                      /* reduced so the form is always visible */
  overflow-y: auto;
  padding: .25rem .5rem 1.25rem .5rem; /* extra bottom padding */
  margin-bottom: .5rem;              /* gap above the input form */
  border:1px solid #eee;
  border-radius:10px;
  background:#fff;
}
.chat-bubble {border-radius:12px; padding:.6rem .8rem; margin:.4rem .2rem; border:1px solid #eee;}
.chat-user {background:#eef7ff;}
.chat-assistant {background:#f6f6f6;}

/* Chat form pinned near bottom */
.chat-form { position: sticky; bottom: 8px; z-index: 11; background: #fff; padding-bottom: .25rem; }
.chat-form .stForm { margin: 0; }
.chat-form .stTextInput>div>div>input { height: 2.4rem; }
.chat-form .stButton>button { height: 2.4rem; }

/* Quill */
.ql-toolbar.ql-snow { position: sticky; top: 0; z-index: 10; background:#fff; border-radius:10px 10px 0 0; }
.ql-container.ql-snow { min-height: 480px; border-radius:0 0 10px 10px; }

/* Buttons */
.toolbar {display:flex; gap:.5rem; flex-wrap:wrap;}
.toolbar .stButton>button {height:2.2rem}

/* Bottom spacing (safety) */
[data-testid="stBottomBlockContainer"] { padding-bottom: 1.25rem; }
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
AUTO_SAVE_SECONDS  = int(os.getenv("AUTO_SAVE_SECONDS", "60"))  # autosave cadence


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

# Robust streamlit-quill wrapper (handles old/new versions)
def render_quill_html(key: str, initial_html: str) -> str:
    try:
        out = st_quill(value=initial_html, placeholder="Write here…", html=True, key=key)
        if isinstance(out, dict) and "html" in out and out["html"]:
            return out["html"]
        if isinstance(out, str) and out:
            return out
    except TypeError:
        try:
            out = st_quill(value=initial_html, placeholder="Write here…", key=key)
        except TypeError:
            out = st_quill(initial_html)
    if isinstance(out, dict):
        if "html" in out and out["html"]:
            return out["html"]
        delta = out.get("delta") or out.get("ops") or {}
        ops = delta.get("ops") if isinstance(delta, dict) else delta
        try:
            text = "".join(op.get("insert", "") for op in ops) if isinstance(ops, list) else ""
        except Exception:
            text = ""
        return "<p>" + text.replace("\n", "</p><p>") + "</p>" if text else (initial_html or "")
    if isinstance(out, str):
        return out
    return initial_html or ""


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
# SUBMISSIONS sheet intentionally not used (you requested no manual submission)

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
    st.session_state.draft_html = ""  # HTML from Quill
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
    """Return last saved draft_html (latest row) for this user+assignment."""
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

# ---------- Top toolbar ----------
tcol1, tcol2, tcol3, tcol4 = st.columns([1.2, 0.9, 1.1, 0.8])
with tcol1:
    st.session_state.assignment_id = st.text_input("Assignment ID", value=st.session_state.assignment_id)
with tcol2:
    if st.button("🔄 Load last draft"):
        loaded_html = load_progress(st.session_state.user_id, st.session_state.assignment_id)
        if loaded_html:
            st.session_state.draft_html = loaded_html
            st.success("Loaded last saved draft.")
            st.rerun()
        else:
            st.warning("No saved draft found.")
with tcol3:
    up = st.file_uploader("Import text/DOCX", type=["txt","docx"], label_visibility="collapsed")
    if up is not None:
        as_text = ""
        if up.type == "text/plain" or up.name.lower().endswith(".txt"):
            as_text = up.read().decode("utf-8", errors="ignore")
        elif up.name.lower().endswith(".docx") and DOCX_OK:
            try:
                d = docx.Document(up)
                as_text = "\n".join([p.text for p in d.paragraphs])
            except Exception as e:
                st.error(f"Failed to read DOCX: {e}")
        if as_text:
            st.session_state.draft_html = "<p>" + as_text.replace("\n", "</p><p>") + "</p>"
            st.success("Imported into editor.")
            st.rerun()
with tcol4:
    if st.button("🧹 Clear chat"):
        st.session_state.chat = []
        st.session_state.llm_outputs = []
        st.toast("Chat cleared")

st.divider()

# ---------- Two-column main: Assistant (left, scrollable) | Draft (right, fixed) ----------
left, right = st.columns([0.5, 0.5], gap="large")
with left:
    st.subheader("💬 Assistant")

    # ---- Render scrollable chat ----
    chat_html = ['<div class="chat-panel">']
    for m in st.session_state.chat:
        css = "chat-user" if m["role"] == "user" else "chat-assistant"
        chat_html.append(f'<div class="chat-bubble {css}">{_html.escape(m["text"])}</div>')
    chat_html.append("</div>")
    st.markdown("".join(chat_html), unsafe_allow_html=True)

    # ---- Auto-scroll to the latest message (keep directly after st.markdown) ----
    components.html(
        """
        <script>
          const p = parent.document.querySelector('.chat-panel');
          if (p) { p.scrollTop = p.scrollHeight; }
        </script>
        """,
        height=0,
    )

    # ---- Chat input (pinned near bottom) ----
    st.markdown('<div class="chat-form">', unsafe_allow_html=True)
    with st.form("chat_form", clear_on_submit=True):
        c1, c2 = st.columns([4,1])
        with c1:
            prompt = st.text_input(
                "Ask for ideas, critique, examples…",
                value="",
                placeholder="Type and press Send",
                label_visibility="collapsed",
            )
        with c2:
            send = st.form_submit_button("Send")
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
    # Rich editor (fixed panel)
    st.session_state.draft_html = render_quill_html("draft_editor", st.session_state.draft_html)

    # Live KPIs
    plain = html_to_text(st.session_state.draft_html)
    k1, k2, k3 = st.columns(3)
    k1.metric("Words", word_count(plain))
    k2.metric("Characters", char_count(plain))
    k3.metric("LLM Responses", len(st.session_state.llm_outputs))

    # Auto-save if changed and cadence reached
    maybe_autosave(st.session_state.draft_html, plain)

    # Draft actions
    bcol1, bcol2, bcol3 = st.columns([1,1,1])
    with bcol1:
        if st.button("💾 Save draft"):
            save_progress(st.session_state.user_id, st.session_state.assignment_id,
                          st.session_state.draft_html, plain, silent=False)
    with bcol2:
        if st.button("📊 Run similarity"):
            if plain.strip() and st.session_state.llm_outputs:
                report = compute_similarity_report(plain, st.session_state.llm_outputs, SIM_THRESHOLD)
                st.session_state.report = report
                st.success(f"Mean: {report['mean']} | High-sim: {report['high_share']*100:.1f}%")
                log_event("similarity_run", f"mean={report['mean']}, high_share={report['high_share']}", "")
            else:
                st.warning("Need draft text + at least one LLM response first.")
    with bcol3:
        if st.button("⬇️ Export evidence (DOCX)"):
            try:
                rep = st.session_state.get("report", {"backend": "none","mean":0,"high_share":0,"rows":[]})
                data = export_evidence_docx(st.session_state.user_id,
                                            st.session_state.assignment_id,
                                            st.session_state.chat,
                                            st.session_state.draft_html,
                                            rep)
                st.download_button("Download DOCX", data=data,
                                   file_name=f"evidence_{st.session_state.user_id}.docx",
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                   use_container_width=True)
                log_event("evidence_export", "", "docx")
            except Exception as e:
                st.error(f"Export failed: {e}")
