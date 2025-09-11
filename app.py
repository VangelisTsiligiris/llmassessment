import streamlit as st
import datetime, time, json, hashlib, io

# --- Optional imports (degrade gracefully) ---
try:
    import gspread
    from gspread.exceptions import WorksheetNotFound
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None

try:
    import google.generativeai as genai
except Exception:
    genai = None

# Optional text extraction for uploads (PDF/DOCX)
PDF_OK = DOCX_OK = False
try:
    from pypdf import PdfReader
    PDF_OK = True
except Exception:
    pass
try:
    import docx
    DOCX_OK = True
except Exception:
    pass

# Optional similarity toolchain (auto-fallback)
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

# ----------------------------
# CONFIG
# ----------------------------
st.set_page_config(page_title="LLM Coursework Helper — Minimal", layout="wide")

SPREADSHEET_KEY = "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ"  # llmassessment
ASSIGNMENT_DEFAULT = "GENERIC"
STORE_FULL_TEXT = False      # data minimisation (only store excerpts in logs)
EXCERPT_CHARS = 400
SIM_THRESHOLD = 0.85

def excerpt(text: str, n: int = EXCERPT_CHARS) -> str:
    if not text: return ""
    t = text.strip()
    return t if STORE_FULL_TEXT or len(t) <= n else (t[:n] + " …")

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

# ----------------------------
# CLIENTS (Gemini + Sheets)
# ----------------------------
# Gemini
if genai is None:
    st.error("google-generativeai not installed.")
    st.stop()

try:
    genai.configure(api_key=st.secrets["google_api"]["gemini_api_key"])
    LLM = genai.GenerativeModel("gemini-1.5-flash")
except Exception as e:
    st.error(f"Gemini setup failed. Check your secrets. Error: {e}")
    st.stop()

# Google Sheets
if gspread is None:
    st.error("gspread / google-auth not installed.")
    st.stop()

try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
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

# ----------------------------
# SESSION STATE
# ----------------------------
if "user_id" not in st.session_state:
    import random, string
    st.session_state.user_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0
if "chat" not in st.session_state:
    st.session_state.chat = []  # [{role, text}]
if "llm_outputs" not in st.session_state:
    st.session_state.llm_outputs = []  # for Voice Report
if "evidence" not in st.session_state:
    st.session_state.evidence = None
if "assignment_id" not in st.session_state:
    st.session_state.assignment_id = ASSIGNMENT_DEFAULT

# ----------------------------
# FUNCTIONS
# ----------------------------
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

def extract_text_from_upload(file) -> str:
    """Return text if we can; else empty string."""
    if not file:
        return ""
    name = file.name.lower()
    data = file.read()
    file.seek(0)
    try:
        if name.endswith(".txt"):
            return data.decode(errors="ignore")
        if name.endswith(".pdf") and PDF_OK:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join([p.extract_text() or "" for p in reader.pages])
        if name.endswith(".docx") and DOCX_OK:
            doc = docx.Document(io.BytesIO(data))
            return "\n".join([p.text for p in doc.paragraphs])
    except Exception:
        pass
    return ""

def segment_paragraphs(text: str):
    if not text: return []
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    out = []
    for p in parts:
        if len(p) <= 600:
            out.append(p)
        else:
            import re
            chunks = re.split(r'(?<=[\.\?\!])\s+', p)
            buf = ""
            for c in chunks:
                if len(buf) + len(c) < 400: buf += (" " + c).strip()
                else:
                    if buf: out.append(buf)
                    buf = c
            if buf: out.append(buf)
    return out

def compute_similarity_report(final_text: str, llm_texts: list[str], sim_thresh: float = SIM_THRESHOLD):
    finals = segment_paragraphs(final_text)
    llm_segs = [s for t in llm_texts for s in segment_paragraphs(t)]
    if not finals or not llm_segs:
        return {"backend": SIM_BACKEND, "mean": 0.0, "high_share": 0.0, "rows": []}

    rows = []
    high_tokens = 0
    total_tokens = sum(len(s.split()) for s in finals)

    if SIM_BACKEND == "sbert":
        Ef = _sbert_model.encode(finals, convert_to_tensor=True, normalize_embeddings=True)
        El = _sbert_model.encode(llm_segs, convert_to_tensor=True, normalize_embeddings=True)
        sims = sbert_util.cos_sim(Ef, El).cpu().numpy()
        from rapidfuzz.distance import Levenshtein
        for i, fseg in enumerate(finals):
            j = int(sims[i].argmax()); s = float(sims[i, j]); nearest = llm_segs[j]
            edit = 1.0 - Levenshtein.normalized_similarity(fseg, nearest)
            rows.append({"final_seg": excerpt(fseg, 300), "nearest_llm": excerpt(nearest, 300),
                         "cosine": round(s, 3), "edit_dist": round(edit, 3)})
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

    else:  # difflib
        def cos_like(a, b):  # 0..1
            from difflib import SequenceMatcher
            return SequenceMatcher(None, a, b).ratio()
        for fseg in finals:
            best, nearest = 0.0, ""
            for l in llm_segs:
                c = cos_like(fseg, l)
                if c > best:
                    best, nearest = c, l
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
        "similarity": sim_summary or {},
        "created_at": datetime.datetime.now().isoformat(),
        "data_minimisation": {"store_full_text": STORE_FULL_TEXT, "excerpt_chars": EXCERPT_CHARS},
    }
    b = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    payload["sha256"] = hashlib.sha256(b).hexdigest()
    return json.dumps(payload, ensure_ascii=False, indent=2)

# ----------------------------
# SIDEBAR
# ----------------------------
st.sidebar.write(f"**User ID:** `{st.session_state.user_id}`")
st.sidebar.text_input("Assignment ID (optional)", key="assignment_id")
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

# ----------------------------
# MAIN — Two tabs
# ----------------------------
tab_chat, tab_draft = st.tabs(["💬 Assistant", "📝 Draft & Submit"])

# --- CHAT TAB ---
with tab_chat:
    st.header("LLM Assistant")
    # render chat history
    for m in st.session_state.chat:
        with st.chat_message(m["role"]):
            st.markdown(m["text"])
    # input
    if prompt := st.chat_input("Ask for ideas, critique, examples, etc."):
        st.session_state.turn_count += 1
        st.session_state.chat.append({"role": "user", "text": prompt})
        with st.chat_message("user"): st.markdown(prompt)

        reply, latency_ms = ask_llm(prompt)
        st.session_state.chat.append({"role": "assistant", "text": reply})
        st.session_state.llm_outputs.append(reply)  # keep for Voice Report
        with st.chat_message("assistant"): st.markdown(reply)

        log_event("prompt", prompt, "", 0)
        log_event("llm_response", prompt, reply, latency_ms)

# --- DRAFT & SUBMIT TAB ---
with tab_draft:
    st.header("Your draft / final")
    up = st.file_uploader("Upload a draft (TXT, PDF, DOCX). We only parse text; files are not stored.", type=["txt","pdf","docx"])
    extracted = ""
    if up:
        extracted = extract_text_from_upload(up)
        if extracted:
            st.success(f"Extracted ~{len(extracted)} chars from {up.name}")
            st.text_area("Extracted text (you can edit it):", value=extracted, height=200, key="extracted_area")
        else:
            st.warning("Couldn’t extract text; paste your draft below.")

    final_text = st.text_area("Paste or write your draft/final here:", height=280, key="final_text_area")

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Run Voice Report"):
            if not final_text.strip() or len(st.session_state.llm_outputs) == 0:
                st.warning("Add final text and generate at least one LLM response first.")
            else:
                report = compute_similarity_report(final_text, st.session_state.llm_outputs, SIM_THRESHOLD)
                st.session_state.report = report
                st.success(f"Similarity backend: {report['backend']}")
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
            words = len(final_text.split())
            chars = len(final_text)
            rep = st.session_state.get("report", {"mean": 0.0, "high_share": 0.0})
            row = [
                datetime.datetime.now().isoformat(),
                st.session_state.user_id,
                st.session_state.assignment_id,
                words, chars,
                sha256(final_text or ""),
                rep.get("mean", 0.0),
                rep.get("high_share", 0.0),
                "",  # notes
            ]
            append_row_safe(SUBMIS_WS, row)
            st.success("Submission logged to Google Sheets.")
