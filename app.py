import os, json, io, time, datetime, hashlib
import streamlit as st

# ---------- Optional libs (graceful fallbacks) ----------
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

# Exports
DOCX_OK = PDF_OK = False
try:
    import docx  # python-docx
    DOCX_OK = True
except Exception:
    pass
try:
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.pagesizes import A4
    PDF_OK = True
except Exception:
    pass

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

# ---------- App config ----------
st.set_page_config(page_title="LLM Coursework Helper — Minimal", layout="wide")


APP_PASSCODE = os.getenv("APP_PASSCODE") or st.secrets.get("env", {}).get("APP_PASSCODE")

if APP_PASSCODE:
    st.session_state.setdefault("__auth_ok", False)
    if not st.session_state["__auth_ok"]:
        st.title("Pilot access")
        code = st.text_input("Enter passcode", type="password")
        if st.button("Enter"):
            if code == APP_PASSCODE:
                st.session_state["__auth_ok"] = True
                st.rerun()   # <-- fixed
            else:
                st.error("Wrong passcode.")
        st.stop()



# Allow env override (good for Cloud Run/Azure)
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY", "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ")
ASSIGNMENT_DEFAULT = os.getenv("ASSIGNMENT_ID", "GENERIC")
STORE_FULL_TEXT = os.getenv("STORE_FULL_TEXT", "false").lower() == "true"
EXCERPT_CHARS = int(os.getenv("EXCERPT_CHARS", "400"))
SIM_THRESHOLD = float(os.getenv("SIM_THRESHOLD", "0.85"))

def excerpt(text: str, n: int = EXCERPT_CHARS) -> str:
    if not text: return ""
    t = text.strip()
    return t if STORE_FULL_TEXT or len(t) <= n else (t[:n] + " …")

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def segment_paragraphs(text: str):
    if not text: return []
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    out = []
    import re
    for p in parts:
        if len(p) <= 600:
            out.append(p)
        else:
            chunks = re.split(r'(?<=[\.\?\!])\s+', p)
            buf = ""
            for c in chunks:
                if len(buf) + len(c) < 400: buf += (" " + c).strip()
                else:
                    if buf: out.append(buf)
                    buf = c
            if buf: out.append(buf)
    return out

# ---------- Secrets loader (env first, then Streamlit secrets) ----------
def load_secrets():
    google_api = st.secrets.get("google_api") if "google_api" in st.secrets else {}
    gemini_key = google_api.get("gemini_api_key") or os.getenv("GEMINI_API_KEY")
    sa_info = (st.secrets.get("gcp_service_account") if "gcp_service_account" in st.secrets else None) \
              or os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    if isinstance(sa_info, str):
        try:
            sa_info = json.loads(sa_info)
        except Exception as e:
            st.error(f"GCP_SERVICE_ACCOUNT_JSON not valid JSON: {e}")
            st.stop()
    return gemini_key, sa_info

GEMINI_KEY, SA_INFO = load_secrets()

# ---------- Clients (Gemini + Sheets) ----------
if genai is None or GEMINI_KEY is None:
    st.error("Gemini client not available or GEMINI_API_KEY missing.")
    st.stop()
try:
    genai.configure(api_key=GEMINI_KEY)
    LLM = genai.GenerativeModel("gemini-1.5-flash")
except Exception as e:
    st.error(f"Gemini setup failed: {e}")
    st.stop()

if gspread is None or SA_INFO is None or Credentials is None:
    st.error("gspread/google-auth not available or service account secrets missing.")
    st.stop()
try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(SA_INFO, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_KEY)
except Exception as e:
    st.error(f"Google Sheets access failed: {e}")
    st.stop()

EVENT_HEADERS = [
    "timestamp","user_id","assignment_id","turn_count","event_type",
    "prompt_excerpt","response_excerpt","prompt_len","response_len",
    "latency_ms","prompt_sha256","response_sha256"
]
SUB_HEADERS = [
    "timestamp","user_id","assignment_id","word_count","char_count",
    "final_sha256","mean_similarity","high_sim_share","notes"
]

def _get_or_create_ws(title, headers):
    try:
        ws = sh.worksheet(title)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
    return ws

EVENTS_WS = _get_or_create_ws("events", EVENT_HEADERS)
SUBMIS_WS = _get_or_create_ws("submissions", SUB_HEADERS)
CONNECT_WS = _get_or_create_ws("connectivity", ["timestamp","user_id","note"])

def append_row_safe(ws, row):
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        st.warning(f"Append failed: {e}")

# ---------- Session ----------
if "user_id" not in st.session_state:
    import random, string
    st.session_state.user_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
if "turn_count" not in st.session_state: st.session_state.turn_count = 0
if "chat" not in st.session_state: st.session_state.chat = []
if "llm_outputs" not in st.session_state: st.session_state.llm_outputs = []
if "evidence" not in st.session_state: st.session_state.evidence = None
if "assignment_id" not in st.session_state: st.session_state.assignment_id = ASSIGNMENT_DEFAULT
if "report" not in st.session_state: st.session_state.report = None

# ---------- Core funcs ----------
def run_connectivity_test():
    ts = datetime.datetime.now().isoformat()
    append_row_safe(CONNECT_WS, [ts, st.session_state.user_id, "ping"])
    return ts

def log_event(event_type: str, prompt: str, response: str, latency_ms: int):
    row = [
        datetime.datetime.now().isoformat(),
        st.session_state.user_id,
        st.session_state.assignment_id,
        st.session_state.turn_count,
        event_type,
        excerpt(prompt),
        excerpt(response),
        len(prompt or ""),
        len(response or ""),
        latency_ms,
        sha256(prompt or ""),
        sha256(response or ""),
    ]
    append_row_safe(EVENTS_WS, row)

def ask_llm(prompt_text: str) -> tuple[str, int]:
    start = time.time()
    chunks = []
    try:
        for ch in LLM.generate_content([prompt_text], stream=True):
            if getattr(ch, "text", None):
                chunks.append(ch.text)
    except Exception as e:
        chunks.append(f"Error calling LLM: {e}")
    latency_ms = round((time.time() - start) * 1000)
    return "".join(chunks), latency_ms

def compute_similarity_report(final_text: str, llm_texts: list[str], sim_thresh: float = SIM_THRESHOLD):
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
        try:
            from rapidfuzz.distance import Levenshtein
            def edit_proxy(a, b): return 1.0 - Levenshtein.normalized_similarity(a, b)
        except Exception:
            from difflib import SequenceMatcher
            def edit_proxy(a, b): return 1.0 - SequenceMatcher(None, a, b).ratio()
        for i, fseg in enumerate(finals):
            j = int(sims[i].argmax()); s = float(sims[i, j]); nearest = llm_segs[j]
            rows.append({"final_seg": excerpt(fseg, 300), "nearest_llm": excerpt(nearest, 300),
                         "cosine": round(s, 3), "edit_dist": round(edit_proxy(fseg, nearest), 3)})
            if s >= sim_thresh: high_tokens += len(fseg.split())

    elif SIM_BACKEND == "tfidf":
        vectorizer = TfidfVectorizer().fit(finals + llm_segs)
        F = vectorizer.transform(finals); L = vectorizer.transform(llm_segs)
        sims = cosine_similarity(F, L)
        for i, fseg in enumerate(finals):
            j = int(sims[i].argmax()); s = float(sims[i, j]); nearest = llm_segs[j]
            words_f, words_l = set(fseg.split()), set(nearest.split())
            jaccard = len(words_f & words_l) / max(1, len(words_f | words_l))
            edit = 1.0 - jaccard
            rows.append({"final_seg": excerpt(fseg, 300), "nearest_llm": excerpt(nearest, 300),
                         "cosine": round(s, 3), "edit_dist": round(edit, 3)})
            if s >= sim_thresh: high_tokens += len(fseg.split())

    else:
        def cos_like(a, b):
            from difflib import SequenceMatcher
            return SequenceMatcher(None, a, b).ratio()
        for fseg in finals:
            best, nearest = 0.0, ""
            for l in llm_segs:
                c = cos_like(fseg, l)
                if c > best: best, nearest = c, l
            edit = 1.0 - cos_like(fseg, nearest)
            rows.append({"final_seg": excerpt(fseg, 300), "nearest_llm": excerpt(nearest, 300),
                         "cosine": round(best, 3), "edit_dist": round(edit, 3)})
            if best >= sim_thresh: high_tokens += len(fseg.split())

    mean_sim = 0.0 if not rows else round(sum(r["cosine"] for r in rows) / len(rows), 3)
    high_share = round(high_tokens / max(1, total_tokens), 3)
    return {"backend": SIM_BACKEND, "mean": mean_sim, "high_share": high_share, "rows": rows[:40]}

def build_evidence_pack(final_text: str | None, sim_summary: dict | None):
    payload = {
        "version": "0.9-min",
        "assignment_id": st.session_state.assignment_id,
        "student": {"pseudonymous_id": st.session_state.user_id},
        "chat": st.session_state.chat if STORE_FULL_TEXT else [
            {"role": m["role"], "text": excerpt(m["text"])} for m in st.session_state.chat
        ],
        "llm_outputs_count": len(st.session_state.llm_outputs),
        "final_sha256": sha256(final_text or ""),
        "writing_alignment": sim_summary or {},
        "created_at": datetime.datetime.now().isoformat(),
        "data_minimisation": {"store_full_text": STORE_FULL_TEXT, "excerpt_chars": EXCERPT_CHARS},
    }
    b = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    payload["sha256"] = hashlib.sha256(b).hexdigest()
    return json.dumps(payload, ensure_ascii=False, indent=2)

# ---------- Exporters ----------
def export_docx_bytes(title: str, text: str) -> bytes:
    if not DOCX_OK: raise RuntimeError("python-docx not installed")
    d = docx.Document()
    if title: d.add_heading(title, level=1)
    for para in text.split("\n"):
        d.add_paragraph(para if para.strip() else "")
    buf = io.BytesIO(); d.save(buf); buf.seek(0); return buf.read()

def export_pdf_bytes(title: str, text: str) -> bytes:
    if not PDF_OK: raise RuntimeError("reportlab not installed")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []
    if title:
        story.append(Paragraph(title, styles["Heading1"]))
        story.append(Spacer(1, 12))
    for para in text.split("\n"):
        story.append(Paragraph((para or " ").replace("  ", " ").strip() or "&nbsp;", styles["BodyText"]))
        story.append(Spacer(1, 6))
    doc.build(story); buf.seek(0); return buf.read()

# ---------- Sidebar ----------
st.sidebar.write(f"**User ID:** `{st.session_state.user_id}`")
st.sidebar.text_input("Assignment ID", key="assignment_id")
if st.sidebar.button("🔧 Sheets connectivity test", use_container_width=True):
    ts = run_connectivity_test()
    st.sidebar.success(f"Write OK @ {ts}")

st.sidebar.markdown("---")
if st.session_state.evidence:
    st.sidebar.download_button(
        "📥 Download Evidence Pack (JSON)",
        data=st.session_state.evidence,
        file_name=f"evidence_{st.session_state.assignment_id}_{st.session_state.user_id}.json",
        mime="application/json",
        use_container_width=True,
    )

# ---------- Main (two tabs) ----------
tab_chat, tab_draft = st.tabs(["💬 Assistant", "📝 Draft & Submit"])

with tab_chat:
    st.header("LLM Assistant")
    for m in st.session_state.chat:
        with st.chat_message(m["role"]):
            st.markdown(m["text"])
    if prompt := st.chat_input("Ask for ideas, critique, examples, etc."):
        st.session_state.turn_count += 1
        st.session_state.chat.append({"role": "user", "text": prompt})
        with st.chat_message("user"): st.markdown(prompt)
        reply, latency_ms = ask_llm(prompt)
        st.session_state.chat.append({"role": "assistant", "text": reply})
        st.session_state.llm_outputs.append(reply)
        with st.chat_message("assistant"): st.markdown(reply)
        log_event("prompt", prompt, "", 0)
        log_event("llm_response", prompt, reply, latency_ms)

with tab_draft:
    st.header("Your draft / final")
    final_text = st.text_area("Paste or write your draft/final here:", height=320, key="final_text_area")
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        if st.button("Run Writing Alignment Report"):
            if not final_text.strip() or len(st.session_state.llm_outputs) == 0:
                st.warning("Add final text and generate at least one LLM response first.")
            else:
                report = compute_similarity_report(final_text, st.session_state.llm_outputs, SIM_THRESHOLD)
                st.session_state.report = report
                st.success(f"Backend: {report['backend']}")
                st.write(f"**Mean similarity**: {report['mean']}  |  **High-sim share (≥{SIM_THRESHOLD})**: {report['high_share']*100:.1f}%")
                with st.expander("Matches (trimmed)"):
                    for r in report["rows"]:
                        st.markdown(f"- **Cos:** {r['cosine']} | **Edit-dist:** {r['edit_dist']}")
                        st.markdown(f"  - Final: {r['final_seg']}")
                        st.markdown(f"  - LLM : {r['nearest_llm']}")
    with c2:
        if st.button("Generate Evidence Pack"):
            rep = st.session_state.get("report")
            st.session_state.evidence = build_evidence_pack(final_text, rep)
            st.success("Evidence Pack generated — download from the sidebar.")
    with c3:
        if st.button("Submit (log to Sheets)"):
            words = len(final_text.split()); chars = len(final_text)
            rep = st.session_state.get("report", {"mean": 0.0, "high_share": 0.0})
            row = [
                datetime.datetime.now().isoformat(),
                st.session_state.user_id,
                st.session_state.assignment_id,
                words, chars,
                sha256(final_text or ""),
                rep.get("mean", 0.0),
                rep.get("high_share", 0.0),
                "",
            ]
            append_row_safe(SUBMIS_WS, row)
            st.success("Submission logged to Google Sheets.")
    with c4:
        if st.button("Export DOCX"):
            try:
                data = export_docx_bytes(f"Coursework — {st.session_state.user_id}", final_text or "")
                st.download_button("⬇️ Download DOCX",
                    data=data, file_name=f"coursework_{st.session_state.user_id}.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", use_container_width=True)
            except Exception as e:
                st.error(f"DOCX export unavailable: {e}")
    with c5:
        if st.button("Export PDF"):
            try:
                data = export_pdf_bytes(f"Coursework — {st.session_state.user_id}", final_text or "")
                st.download_button("⬇️ Download PDF",
                    data=data, file_name=f"coursework_{st.session_state.user_id}.pdf",
                    mime="application/pdf", use_container_width=True)
            except Exception as e:
                st.error(f"PDF export unavailable: {e}")
