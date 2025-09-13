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

# HTML → text
try:
    from bs4 import BeautifulSoup
    def html_to_text(html: str) -> str:
        return BeautifulSoup(html or "", "html.parser").get_text("\n")
except Exception:
    def html_to_text(html: str) -> str:
        return (html or "").replace("<br>", "\n").replace("<br/>", "\n")

# ---------- Page config ----------
st.set_page_config(page_title="LLM Coursework Helper", layout="wide")

# ---------- CSS (no sticky; clear, bordered boxes) ----------
st.markdown("""
<style>
.block-container {padding-top: 1rem; padding-bottom: 1rem;}
.header-bar {display:flex; gap:.75rem; flex-wrap:wrap; font-size:.95rem; color:#444; margin-bottom:.25rem;}
.status-chip{background:#f5f7fb;border:1px solid #e6e9f2;border-radius:999px;padding:.15rem .6rem}
.small-muted{color:#7a7f8a}

/* Two panes with fixed working height */
.left-col, .right-col {display:flex; flex-direction:column; height:68vh; gap:.6rem}

/* Assistant pane */
.chat-box {
  flex:1; min-height:320px; overflow-y:auto;
  border:1px solid #dcdfe6; border-radius:10px; background:#fff; padding:.5rem;
}
.chat-bubble {border-radius:12px; padding:.6rem .8rem; margin:.4rem .2rem; border:1px solid #eee; line-height:1.45}
.chat-user {background:#eef7ff;}
.chat-assistant {background:#f6f6f6;}
.prompt-row {
  border:1px solid #e6e9f2; background:#fafafa; border-radius:10px; padding:.6rem .6rem;
}

/* Draft editor */
.editor-wrapper{
  flex:1; min-height:320px;
  border:1px solid #dcdfe6; border-radius:10px; background:#fff; padding:.25rem .5rem;
}
.editor-inner{height:520px; overflow:auto;}
.ql-container.ql-snow {border:none;}
.ql-toolbar.ql-snow {border:none; border-bottom:1px solid #ddd;}
</style>
""", unsafe_allow_html=True)

# ---------- Pilot gate ----------
def _gen_id(n=6): return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))
APP_PASSCODE = os.getenv("APP_PASSCODE") or st.secrets.get("env", {}).get("APP_PASSCODE")
st.session_state.setdefault("__auth_ok", False)
st.session_state.setdefault("user_id", None)

if APP_PASSCODE and not st.session_state["__auth_ok"]:
    st.title("Pilot access")
    c1, c2 = st.columns([2,2])
    with c1:
        passcode = st.text_input("Enter passcode", type="password")
    with c2:
        uid = st.text_input("Your User ID (keep this to resume)", value=st.session_state.get("user_id") or "")
        if st.button("Generate new ID"):
            uid = _gen_id()
            st.session_state["user_id"] = uid
            st.info(f"New ID: **{uid}** — please save it.")
            st.rerun()
    if st.button("Enter"):
        if passcode == APP_PASSCODE:
            if not uid.strip():
                uid = _gen_id()
            st.session_state["user_id"] = uid.strip().upper()
            st.session_state["__auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong passcode.")
    st.stop()

# ---------- Env ----------
SPREADSHEET_KEY    = os.getenv("SPREADSHEET_KEY", "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ")
ASSIGNMENT_DEFAULT = os.getenv("ASSIGNMENT_ID", "GENERIC")
SIM_THRESHOLD      = float(os.getenv("SIM_THRESHOLD", "0.85"))
AUTO_SAVE_SECONDS  = int(os.getenv("AUTO_SAVE_SECONDS", "60"))

# ---------- Helpers ----------
def excerpt(text, n=300):
    t = text or ""
    return t if len(t) <= n else t[:n] + " …"
def word_count(t): return len((t or "").split())
def char_count(t): return len(t or "")

def render_quill_html(key: str, initial_html: str) -> str:
    """Robust wrapper for streamlit-quill across versions."""
    try:
        out = st_quill(value=initial_html, placeholder="Write here…", html=True, key=key)
        if isinstance(out, dict) and out.get("html"): return out["html"]
        if isinstance(out, str) and out: return out
    except TypeError:
        try:
            out = st_quill(value=initial_html, placeholder="Write here…", key=key)
        except TypeError:
            out = st_quill(initial_html)
    if isinstance(out, dict):
        if out.get("html"): return out["html"]
        delta = out.get("delta") or out.get("ops") or {}
        ops = delta.get("ops") if isinstance(delta, dict) else delta
        try:
            text = "".join(op.get("insert", "") for op in ops) if isinstance(ops, list) else ""
        except Exception:
            text = ""
        return "<p>" + text.replace("\n", "</p><p>") + "</p>" if text else (initial_html or "")
    if isinstance(out, str): return out
    return initial_html or ""

# ---------- Secrets / clients ----------
def load_secrets():
    google_api = st.secrets.get("google_api", {})
    gemini_key = google_api.get("gemini_api_key") or os.getenv("GEMINI_API_KEY")
    sa_info = st.secrets.get("gcp_service_account") or os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    if isinstance(sa_info, str):
        try: sa_info = json.loads(sa_info)
        except Exception:
            st.error("Invalid service account JSON"); st.stop()
    return gemini_key, sa_info

GEMINI_KEY, SA_INFO = load_secrets()

if genai is None or not GEMINI_KEY: st.error("Gemini not available / missing key."); st.stop()
try:
    genai.configure(api_key=GEMINI_KEY)
    LLM = genai.GenerativeModel("gemini-1.5-flash")
except Exception as e:
    st.error(f"Gemini setup failed: {e}"); st.stop()

if gspread is None or SA_INFO is None or Credentials is None:
    st.error("Google Sheets client not available."); st.stop()
try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(SA_INFO, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_KEY)
except Exception as e:
    st.error(f"Google Sheets access failed: {e}"); st.stop()

def _get_or_create_ws(title, headers):
    try: return sh.worksheet(title)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws

EVENTS_WS = _get_or_create_ws("events", ["timestamp","user_id","assignment_id","turn_count","event_type","prompt","response"])
DRAFTS_WS = _get_or_create_ws("drafts", ["user_id","assignment_id","draft_html","draft_text","last_updated"])

def append_row_safe(ws, row):
    try: ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e: st.warning(f"Append failed: {e}")

# ---------- Session ----------
if not st.session_state.get("user_id"): st.session_state["user_id"] = _gen_id()
st.session_state.setdefault("assignment_id", ASSIGNMENT_DEFAULT)
st.session_state.setdefault("chat", [])
st.session_state.setdefault("llm_outputs", [])
st.session_state.setdefault("draft_html", "")
st.session_state.setdefault("report", None)
st.session_state.setdefault("last_saved_at", None)
st.session_state.setdefault("last_autosave_at", None)
st.session_state.setdefault("last_saved_html", "")

# ---------- Core ----------
def ask_llm(prompt_text: str):
    start = time.time()
    out = []
    try:
        for ch in LLM.generate_content([prompt_text], stream=True):
            if getattr(ch, "text", None): out.append(ch.text)
    except Exception as e:
        out.append(f"Error: {e}")
    return "".join(out), round((time.time() - start)*1000)

def log_event(event_type: str, prompt: str, response: str):
    append_row_safe(EVENTS_WS, [
        datetime.datetime.now().isoformat(),
        st.session_state.user_id,
        st.session_state.assignment_id,
        len(st.session_state.chat),
        event_type, excerpt(prompt, 500), excerpt(response, 1000)
    ])

def save_progress(user_id, assignment_id, draft_html, draft_text, silent=False):
    append_row_safe(DRAFTS_WS, [user_id, assignment_id, draft_html, draft_text, datetime.datetime.now().isoformat()])
    st.session_state.last_saved_at = datetime.datetime.now()
    st.session_state.last_saved_html = draft_html
    if not silent: st.toast("Draft saved")

def load_progress(user_id, assignment_id):
    try:
        rows = DRAFTS_WS.get_all_records()
        for r in reversed(rows):
            if str(r.get("user_id","")).upper().strip()==user_id.upper().strip() and str(r.get("assignment_id","")).strip()==assignment_id.strip():
                return r.get("draft_html") or ""
    except Exception: return ""
    return ""

def maybe_autosave(draft_html, draft_text):
    now = time.time()
    last = st.session_state.last_autosave_at or 0
    changed = (draft_html or "") != (st.session_state.last_saved_html or "")
    if changed and (now - last) >= AUTO_SAVE_SECONDS:
        save_progress(st.session_state.user_id, st.session_state.assignment_id, draft_html, draft_text, silent=True)
        st.session_state.last_autosave_at = now

def compute_similarity_report(final_text, llm_texts, sim_thresh=SIM_THRESHOLD):
    finals = [p for p in (segment_paragraphs(final_text)) if p]
    llm_segs = [s for t in llm_texts for s in segment_paragraphs(t)]
    if not finals or not llm_segs:
        return {"backend": SIM_BACKEND, "mean": 0.0, "high_share": 0.0, "rows": []}
    rows, high_tokens = [], 0
    total_tokens = sum(len(s.split()) for s in finals)

    if SIM_BACKEND == "sbert":
        Ef = _sbert_model.encode(finals, convert_to_tensor=True, normalize_embeddings=True)
        El = _sbert_model.encode(llm_segs, convert_to_tensor=True, normalize_embeddings=True)
        sims = sbert_util.cos_sim(Ef, El).cpu().numpy()
        for i, f in enumerate(finals):
            j = int(sims[i].argmax()); s = float(sims[i, j]); l = llm_segs[j]
            rows.append({"final_seg": excerpt(f, 200), "nearest_llm": excerpt(l, 200), "cosine": round(s, 3)})
            if s >= sim_thresh: high_tokens += len(f.split())
    elif SIM_BACKEND == "tfidf":
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        vec = TfidfVectorizer().fit(finals + llm_segs)
        F = vec.transform(finals); L = vec.transform(llm_segs)
        sims = cosine_similarity(F, L)
        for i, f in enumerate(finals):
            j = int(sims[i].argmax()); s = float(sims[i, j]); l = llm_segs[j]
            rows.append({"final_seg": excerpt(f, 200), "nearest_llm": excerpt(l, 200), "cosine": round(s, 3)})
            if s >= sim_thresh: high_tokens += len(f.split())
    else:
        from difflib import SequenceMatcher
        def cos_like(a, b): return SequenceMatcher(None, a, b).ratio()
        for f in finals:
            best, near = 0.0, ""
            for l in llm_segs:
                c = cos_like(f, l)
                if c > best: best, near = c, l
            rows.append({"final_seg": excerpt(f, 200), "nearest_llm": excerpt(near, 200), "cosine": round(best, 3)})
            if best >= sim_thresh: high_tokens += len(f.split())

    mean_sim = round(sum(r["cosine"] for r in rows)/len(rows), 3) if rows else 0.0
    high_share = round(high_tokens/max(1, total_tokens), 3)
    return {"backend": SIM_BACKEND, "mean": mean_sim, "high_share": high_share, "rows": rows[:30]}

def export_evidence_docx(user_id, assignment_id, chat, draft_html, report):
    if not DOCX_OK: raise RuntimeError("python-docx not installed")
    final_text = html_to_text(draft_html)
    d = docx.Document()
    d.add_heading("Coursework Evidence Pack", 0)
    d.add_paragraph(f"User ID: {user_id}")
    d.add_paragraph(f"Assignment ID: {assignment_id}")
    d.add_paragraph(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    d.add_heading("Chat with LLM", 1)
    if not chat:
        d.add_paragraph("(No interactions logged)")
    for m in chat:
        d.add_paragraph(f"{'Student' if m['role']=='user' else 'LLM'}: {m['text']}")
    d.add_heading("Final Draft (extract)", 1)
    for para in final_text.split("\n"): d.add_paragraph(para)
    d.add_heading("Similarity Report", 1)
    d.add_paragraph(f"Backend: {report.get('backend','-')}")
    d.add_paragraph(f"Mean similarity: {report.get('mean',0.0)}")
    d.add_paragraph(f"High-sim share: {report.get('high_share',0.0)*100:.1f}%")
    for r in report.get("rows", []):
        d.add_paragraph(f"- Cos: {r['cosine']} | Final: {r['final_seg']} | LLM: {r['nearest_llm']}")
    buf = io.BytesIO(); d.save(buf); buf.seek(0); return buf.read()

# ---------- Header ----------
st.markdown(
    f"""<div class="header-bar">
         <div class="status-chip">User: {st.session_state.user_id}</div>
         <div class="status-chip">Assignment: {st.session_state.assignment_id}</div>
         <div class="status-chip">Similarity: {SIM_BACKEND}</div>
         <div class="small-muted">Last saved: {st.session_state.last_saved_at.strftime('%H:%M:%S') if st.session_state.last_saved_at else '—'}</div>
       </div>""",
    unsafe_allow_html=True
)

# ---------- Toolbar ----------
t1, t2, t3, t4 = st.columns([1.2, 0.9, 1.1, 0.8])
with t1:
    st.session_state.assignment_id = st.text_input("Assignment ID", value=st.session_state.assignment_id)
with t2:
    if st.button("🔄 Load last draft"):
        html = load_progress(st.session_state.user_id, st.session_state.assignment_id)
        if html:
            st.session_state.draft_html = html
            st.success("Loaded last saved draft.")
            st.rerun()
        else:
            st.warning("No saved draft found.")
with t3:
    up = st.file_uploader("Import text/DOCX", type=["txt","docx"], label_visibility="collapsed")
    if up is not None:
        as_text = ""
        if up.type == "text/plain" or up.name.lower().endswith(".txt"):
            as_text = up.read().decode("utf-8", errors="ignore")
        elif up.name.lower().endswith(".docx") and DOCX_OK:
            try:
                d = docx.Document(up); as_text = "\n".join(p.text for p in d.paragraphs)
            except Exception as e:
                st.error(f"Failed to read DOCX: {e}")
        if as_text:
            st.session_state.draft_html = "<p>" + as_text.replace("\n", "</p><p>") + "</p>"
            st.success("Imported into editor."); st.rerun()
with t4:
    if st.button("🧹 Clear chat"):
        st.session_state.chat = []; st.session_state.llm_outputs = []; st.toast("Chat cleared")

st.divider()

# ---------- Main two columns ----------
left, right = st.columns([0.5, 0.5], gap="large")

# LEFT — Assistant
with left:
    st.subheader("💬 Assistant")
    st.markdown('<div class="left-col">', unsafe_allow_html=True)

    # Chat window
    chat_html = ['<div class="chat-box">']
    if not st.session_state.chat:
        chat_html.append('<div class="chat-bubble" style="background:#fbfbfb;color:#708090;border-style:dashed">'
                         'No messages yet — ask for ideas, critique, or examples.</div>')
    else:
        for m in st.session_state.chat:
            css = "chat-user" if m["role"] == "user" else "chat-assistant"
            chat_html.append(f'<div class="chat-bubble {css}">{_html.escape(m["text"] or "")}</div>')
    chat_html.append("</div>")
    st.markdown("".join(chat_html), unsafe_allow_html=True)

    # Prompt (non-sticky, always visible under the chat box)
    with st.form("chat_form", clear_on_submit=True):
        st.markdown('<div class="prompt-row">', unsafe_allow_html=True)
        c1, c2 = st.columns([4,1])
        with c1:
            prompt = st.text_input("Ask…", "", placeholder="Type and press Send", label_visibility="collapsed")
        with c2:
            send = st.form_submit_button("Send")
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)

    if send and prompt.strip():
        st.session_state.chat.append({"role": "user", "text": prompt})
        reply, _lat = ask_llm(prompt)
        st.session_state.chat.append({"role": "assistant", "text": reply})
        st.session_state.llm_outputs.append(reply)
        log_event("chat_user", prompt, "")
        log_event("chat_llm", prompt, reply)
        st.rerun()

# RIGHT — Draft
with right:
    st.subheader("📝 Draft")
    st.markdown('<div class="right-col">', unsafe_allow_html=True)

    st.markdown('<div class="editor-wrapper"><div class="editor-inner">', unsafe_allow_html=True)
    st.session_state.draft_html = render_quill_html("editor", st.session_state.draft_html)
    st.markdown('</div></div>', unsafe_allow_html=True)

    plain = html_to_text(st.session_state.draft_html)
    k1, k2, k3 = st.columns(3)
    k1.metric("Words", word_count(plain))
    k2.metric("Characters", char_count(plain))
    k3.metric("LLM Responses", len(st.session_state.llm_outputs))

    maybe_autosave(st.session_state.draft_html, plain)

    a1, a2, a3 = st.columns(3)
    with a1:
        if st.button("💾 Save draft"):
            save_progress(st.session_state.user_id, st.session_state.assignment_id, st.session_state.draft_html, plain, silent=False)
    with a2:
        if st.button("📊 Run similarity"):
            if plain.strip() and st.session_state.llm_outputs:
                st.session_state.report = compute_similarity_report(plain, st.session_state.llm_outputs, SIM_THRESHOLD)
                r = st.session_state.report
                st.success(f"Mean: {r['mean']} | High-sim: {r['high_share']*100:.1f}%")
                log_event("similarity_run", f"mean={r['mean']}, high_share={r['high_share']}", "")
            else:
                st.warning("Need draft text + at least one LLM response first.")
    with a3:
        if st.button("⬇️ Export evidence (DOCX)"):
            try:
                rep = st.session_state.get("report", {"backend": "none","mean":0,"high_share":0,"rows":[]})
                data = export_evidence_docx(st.session_state.user_id, st.session_state.assignment_id, st.session_state.chat, st.session_state.draft_html, rep)
                st.download_button("Download DOCX", data=data, file_name=f"evidence_{st.session_state.user_id}.docx",
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                   use_container_width=True)
                log_event("evidence_export", "", "docx")
            except Exception as e:
                st.error(f"Export failed: {e}")

    st.markdown('</div>', unsafe_allow_html=True)
