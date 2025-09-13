import os, io, json, time, datetime, hashlib, random, string
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

# For HTML→text (Quill content)
try:
    from bs4 import BeautifulSoup
    def html_to_text(html: str) -> str:
        return BeautifulSoup(html or "", "html.parser").get_text("\n")
except Exception:
    def html_to_text(html: str) -> str:
        return (html or "").replace("<br>", "\n").replace("<br/>", "\n")

# ---------- App config ----------
st.set_page_config(page_title="LLM Coursework Helper", layout="wide")

# ---------- Simple pilot gate with User ID entry ----------
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
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY", "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ")
ASSIGNMENT_DEFAULT = os.getenv("ASSIGNMENT_ID", "GENERIC")
SIM_THRESHOLD = float(os.getenv("SIM_THRESHOLD", "0.85"))

# ---------- Helpers ----------
def excerpt(text, n=300):
    t = text or ""
    return t if len(t) <= n else t[:n] + " …"

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def segment_paragraphs(text: str):
    if not text:
        return []
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    return parts

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
SUBMIS_WS = _get_or_create_ws("submissions", ["timestamp","user_id","assignment_id","word_count","char_count","final_sha256","mean_similarity","high_share"])

def append_row_safe(ws, row):
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        st.warning(f"Append failed: {e}")

# ---------- Session (initial) ----------
if not st.session_state.get("user_id"):
    # For cases where APP_PASSCODE is not set
    st.session_state["user_id"] = _gen_id()
if "assignment_id" not in st.session_state:
    st.session_state.assignment_id = ASSIGNMENT_DEFAULT
if "chat" not in st.session_state:
    st.session_state.chat = []
if "llm_outputs" not in st.session_state:
    st.session_state.llm_outputs = []
if "draft_html" not in st.session_state:
    st.session_state.draft_html = ""  # rich text (HTML)
if "report" not in st.session_state:
    st.session_state.report = None

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

def save_progress(user_id, assignment_id, draft_html, draft_text):
    row = [user_id, assignment_id, draft_html, draft_text, datetime.datetime.now().isoformat()]
    append_row_safe(DRAFTS_WS, row)

def load_progress(user_id, assignment_id):
    """Return last saved draft_html (latest row) for this user+assignment."""
    try:
        # Latest match if we iterate from bottom
        records = DRAFTS_WS.get_all_records()
        for r in reversed(records):
            if str(r.get("user_id","")).strip().upper() == user_id.strip().upper() and str(r.get("assignment_id","")).strip() == assignment_id.strip():
                return r.get("draft_html") or ""
    except Exception:
        return ""
    return ""

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
    d.add_heading("Coursework Evidence Report", 0)
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

# ---------- Sidebar ----------
st.sidebar.write(f"**User ID:** `{st.session_state.user_id}`")
st.sidebar.text_input("Assignment ID", key="assignment_id")

# Quick load last saved (convenience)
if st.sidebar.button("🔄 Load last saved draft"):
    loaded_html = load_progress(st.session_state.user_id, st.session_state.assignment_id)
    if loaded_html:
        st.session_state.draft_html = loaded_html
        st.sidebar.success("Loaded last saved draft.")
        st.rerun()
    else:
        st.sidebar.warning("No saved draft found.")

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
    # Rich editor returns HTML when html=True
    st.session_state.draft_html = st_quill(
        value=st.session_state.draft_html,
        placeholder="Write here…",
        html=True,
        key="draft_editor",
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("💾 Save draft"):
            plain = html_to_text(st.session_state.draft_html)
            save_progress(st.session_state.user_id, st.session_state.assignment_id,
                          st.session_state.draft_html, plain)
            st.success("Draft saved")
    with col2:
        if st.button("🔄 Load last saved draft (tab)"):
            loaded_html = load_progress(st.session_state.user_id, st.session_state.assignment_id)
            if loaded_html:
                st.session_state.draft_html = loaded_html
                st.success("Loaded last saved draft.")
                st.rerun()
            else:
                st.warning("No saved draft found.")

with tab_submit:
    st.header("Evidence & Submission")
    final_plain = html_to_text(st.session_state.draft_html)

    if st.button("📊 Run Similarity Report"):
        if final_plain.strip() and st.session_state.llm_outputs:
            report = compute_similarity_report(final_plain, st.session_state.llm_outputs, SIM_THRESHOLD)
            st.session_state.report = report
            st.success(f"Mean: {report['mean']} | High-sim share: {report['high_share']*100:.1f}%")
            with st.expander("Detailed matches"):
                for r in report["rows"]:
                    st.markdown(f"- Cosine {r['cosine']}: Final `{r['final_seg']}` vs LLM `{r['nearest_llm']}`")
        else:
            st.warning("Need draft text + at least one LLM response first.")

    if st.button("📤 Submit to Sheets"):
        words = len(final_plain.split()); chars = len(final_plain)
        rep = st.session_state.get("report", {"mean": 0.0, "high_share": 0.0})
        append_row_safe(SUBMIS_WS, [datetime.datetime.now().isoformat(),
                                    st.session_state.user_id,
                                    st.session_state.assignment_id,
                                    words, chars,
                                    sha256(final_plain),
                                    rep.get("mean",0.0),
                                    rep.get("high_share",0.0)])
        st.success("Submission logged")

    if st.button("⬇️ Export Evidence as DOCX"):
        try:
            rep = st.session_state.get("report", {"backend": "none","mean":0,"high_share":0,"rows":[]})
            data = export_evidence_docx(st.session_state.user_id,
                                        st.session_state.assignment_id,
                                        st.session_state.chat,
                                        st.session_state.draft_html,
                                        rep)
            st.download_button("Download DOCX", data=data,
                               file_name=f"evidence_{st.session_state.user_id}.docx",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        except Exception as e:
            st.error(f"Export failed: {e}")
