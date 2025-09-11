import streamlit as st
import re, random, string, datetime, time, json, hashlib
from typing import List, Dict, Tuple, Optional

# --- 3rd party ---
import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# Optional UX: copy button (safe fallback if not installed)
try:
    from st_copy_to_clipboard import st_copy_to_clipboard
except Exception:
    def st_copy_to_clipboard(*args, **kwargs):  # no-op
        pass

# Optional similarity toolchain (we'll degrade gracefully if missing)
SIM_BACKEND = "none"
try:
    from sentence_transformers import SentenceTransformer, util as sbert_util
    from rapidfuzz.distance import Levenshtein
    _sbert_model = SentenceTransformer("all-MiniLM-L6-v2")
    SIM_BACKEND = "sbert"
except Exception:
    try:
        # light fallback (no GPU): TF-IDF + cosine
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        SIM_BACKEND = "tfidf"
    except Exception:
        from difflib import SequenceMatcher
        SIM_BACKEND = "difflib"

# ----------------------------
# CONFIG
# ----------------------------
st.set_page_config(page_title="LLM Assessment — Prototype", layout="wide")

SPREADSHEET_KEY = "1i9kIMnIJkbpOWsqKtcyuTfz-5BREKPNXqESjtWJiDuQ"  # llmassessment
STORE_FULL_TEXT = False        # Data minimisation; keep False for production pilots
EXCERPT_CHARS = 500            # Max chars to store when STORE_FULL_TEXT=False
SIM_THRESHOLD = 0.85           # High-sim threshold for voice report
ASSIGNMENT = {
    "id": "GEN_CW_001",
    "title": "Coursework (Prototype)",
    "milestones": [
        {"id": "M0", "name": "Plan (aims & approach)", "require_note": True},
        {"id": "M1", "name": "Concept checks (Q&A)", "require_note": True},
        {"id": "M2", "name": "Outline (claims–evidence)", "require_note": True},
        {"id": "M3", "name": "First draft", "require_note": False},
        {"id": "M4", "name": "Critical revisions (why/what)", "require_note": True},
        {"id": "M5", "name": "References & integrity note", "require_note": True},
        {"id": "M6", "name": "Final synthesis + voice report", "require_note": True},
    ],
}

EVENT_HEADERS = [
    "timestamp","user_id","assignment_id","turn_count","event_type","milestone_id",
    "intent","attachment_type","prompt","response","prompt_len","response_len",
    "latency_ms","prompt_sha256","response_sha256","flags","ui_context"
]

ARTIFACT_HEADERS = [
    "timestamp","user_id","assignment_id","artifact_kind","artifact_id",
    "text_excerpt_or_full","text_hash","length","segmented"
]

# Intent rules (starter taxonomy)
INTENT_RULES = {
  "Understand": r"\b(explain|what is|define|overview|summar(y|ise))\b",
  "Clarify": r"\b(clarify|difference between|vs\.|compare|distinguish)\b",
  "Outline": r"\b(outline|structure|headings|plan)\b",
  "Critique": r"\b(critique|evaluate|limitations|counter(argument|point)|weakness)\b",
  "Examples": r"\b(example|case study|illustrate)\b",
  "Test/Verify": r"\b(check|verify|test|validate|evidence|support)\b",
  "Transform": r"\b(rewrite|paraphrase|simplify|tone|refine)\b",
  "Cite": r"\b(citation|source|doi|reference|harvard|apa)\b",
  "Compute/Code": r"\b(code|python|formula|equation|calculate|compute)\b",
}

# ----------------------------
# UTILS
# ----------------------------
def generate_short_id(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def excerpt(text: str, n: int = EXCERPT_CHARS) -> str:
    if not text: return ""
    t = text.strip()
    return t if STORE_FULL_TEXT or len(t) <= n else (t[:n] + " …")

def classify_intent(prompt: str) -> str:
    p = (prompt or "").lower()
    for label, pattern in INTENT_RULES.items():
        if re.search(pattern, p):
            return label
    return "Other"

def segment_paragraphs(text: str) -> List[str]:
    if not text: return []
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    # split very long paragraphs into ~2–3 chunks by sentence-ish separators
    out = []
    for p in parts:
        if len(p) <= 600:
            out.append(p)
        else:
            chunks = re.split(r'(?<=[\.\?\!])\s+', p)
            buff = ""
            for c in chunks:
                if len(buff) + len(c) < 400: buff += (" " + c).strip()
                else:
                    if buff: out.append(buff)
                    buff = c
            if buff: out.append(buff)
    return out

# ----------------------------
# CLIENTS (Gemini + Sheets)
# ----------------------------
try:
    genai.configure(api_key=st.secrets["google_api"]["gemini_api_key"])
    GEMINI = genai.GenerativeModel("gemini-1.5-flash")
except Exception as e:
    st.error(f"Gemini setup failed. Check [google_api] in secrets. Error: {e}")
    st.stop()

try:
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_KEY)
except Exception as e:
    st.error(f"Google Sheets access failed. Share the sheet with your service account. Error: {e}")
    st.stop()

def _get_or_create_ws(title: str, headers: List[str] | None = None):
    try:
        ws = sh.worksheet(title)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1, cols=(len(headers) if headers else 10))
        if headers:
            ws.append_row(headers, value_input_option="USER_ENTERED")
    return ws

EVENTS_WS = _get_or_create_ws("events", EVENT_HEADERS)
ARTIFACTS_WS = _get_or_create_ws("artifacts", ARTIFACT_HEADERS)
CONNECT_WS = _get_or_create_ws("connectivity", ["timestamp","user_id","note"])

# ----------------------------
# SESSION STATE
# ----------------------------
if "user_id" not in st.session_state:
    st.session_state.user_id = generate_short_id()
if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0
if "milestone_index" not in st.session_state:
    st.session_state.milestone_index = 0
if "events_cache" not in st.session_state:
    st.session_state.events_cache = []
if "artifacts_cache" not in st.session_state:
    st.session_state.artifacts_cache = []
if "evidence_json" not in st.session_state:
    st.session_state.evidence_json = None
if "last_draft_len" not in st.session_state:
    st.session_state.last_draft_len = {}

# ----------------------------
# PERSISTENCE HELPERS
# ----------------------------
def append_row_safe(ws, row):
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        st.warning(f"Append failed: {e}")

def emit_event(event: Dict):
    # local cache
    st.session_state.events_cache.append(event)
    # row to sheet
    row = [
        event.get("timestamp"),
        event.get("user_id"),
        event.get("assignment_id"),
        event.get("turn_count", 0),
        event.get("event_type"),
        event.get("milestone_id", ""),
        event.get("intent",""),
        event.get("attachment_type",""),
        event.get("prompt",""),
        event.get("response",""),
        len(event.get("prompt","") or ""),
        len(event.get("response","") or ""),
        event.get("latency_ms", 0),
        sha256(event.get("prompt","")),
        sha256(event.get("response","")),
        event.get("flags",""),
        event.get("ui_context",""),
    ]
    append_row_safe(EVENTS_WS, row)

def store_artifact(kind: str, text: str, segmented: bool=False) -> str:
    """Stores LLM output or drafts as artifacts; returns artifact_id (hash)."""
    aid = sha256(text or "")
    row = [
        datetime.datetime.now().isoformat(),
        st.session_state.user_id,
        ASSIGNMENT["id"],
        kind,
        aid,
        (text if STORE_FULL_TEXT else excerpt(text)),
        sha256(text or ""),
        len(text or ""),
        str(bool(segmented)),
    ]
    st.session_state.artifacts_cache.append({
        "kind": kind, "artifact_id": aid, "text": text, "segmented": segmented
    })
    append_row_safe(ARTIFACTS_WS, row)
    return aid

def run_connectivity_test():
    try:
        ts = datetime.datetime.now().isoformat()
        append_row_safe(CONNECT_WS, [ts, st.session_state.user_id, "ping"])
        return True, ts
    except Exception as e:
        return False, str(e)

# ----------------------------
# LLM CALL
# ----------------------------
def ask_gemini(prompt_text: str) -> Tuple[str, int]:
    start = time.time()
    chunks = []
    try:
        for chunk in GEMINI.generate_content([prompt_text], stream=True):
            if getattr(chunk, "text", None):
                chunks.append(chunk.text)
    except Exception as e:
        chunks.append(f"Error calling Gemini: {e}")
    latency_ms = round((time.time() - start) * 1000)
    return "".join(chunks), latency_ms

# ----------------------------
# EVIDENCE PACK
# ----------------------------
def build_evidence_pack() -> str:
    payload = {
        "version": "1.1",
        "assignment": ASSIGNMENT,
        "student": {"pseudonymous_id": st.session_state.user_id},
        "events": st.session_state.events_cache,
        "artifacts": [
            {k: v for k, v in a.items() if (k != "text" or STORE_FULL_TEXT)}
            for a in st.session_state.artifacts_cache
        ],
        "sim_backend": SIM_BACKEND,
        "created_at": datetime.datetime.now().isoformat(),
        "data_minimisation": {"store_full_text": STORE_FULL_TEXT, "excerpt_chars": EXCERPT_CHARS},
    }
    b = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    payload["sha256"] = hashlib.sha256(b).hexdigest()
    return json.dumps(payload, ensure_ascii=False, indent=2)

# ----------------------------
# SIMILARITY / VOICE REPORT
# ----------------------------
def compute_similarity_report(final_text: str, llm_texts: List[str], sim_thresh: float = SIM_THRESHOLD):
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
        for i, fseg in enumerate(finals):
            j = int(sims[i].argmax())
            s = float(sims[i, j])
            nearest = llm_segs[j]
            edit = 1.0 - Levenshtein.normalized_similarity(fseg, nearest)  # 0..1 (1 = very different)
            rows.append({
                "final_seg": excerpt(fseg, 300),
                "nearest_llm": excerpt(nearest, 300),
                "cosine": round(s, 3),
                "edit_dist": round(edit, 3)
            })
            if s >= sim_thresh:
                high_tokens += len(fseg.split())

    elif SIM_BACKEND == "tfidf":
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        vectorizer = TfidfVectorizer().fit(finals + llm_segs)
        F = vectorizer.transform(finals)
        L = vectorizer.transform(llm_segs)
        sims = cosine_similarity(F, L)

        for i, fseg in enumerate(finals):
            j = int(sims[i].argmax())
            s = float(sims[i, j])
            nearest = llm_segs[j]
            # cheap edit proxy: 1 - Jaccard over word sets
            words_f = set(fseg.split())
            words_l = set(nearest.split())
            jaccard = len(words_f & words_l) / max(1, len(words_f | words_l))
            edit = 1.0 - jaccard
            rows.append({
                "final_seg": excerpt(fseg, 300),
                "nearest_llm": excerpt(nearest, 300),
                "cosine": round(s, 3),
                "edit_dist": round(edit, 3)
            })
            if s >= sim_thresh:
                high_tokens += len(fseg.split())

    else:  # difflib fallback
        from difflib import SequenceMatcher
        def cos_like(a, b):  # 0..1
            return SequenceMatcher(None, a, b).ratio()
        for fseg in finals:
            best = 0.0; nearest = ""
            for l in llm_segs:
                c = cos_like(fseg, l)
                if c > best:
                    best = c; nearest = l
            edit = 1.0 - cos_like(fseg, nearest)
            rows.append({
                "final_seg": excerpt(fseg, 300),
                "nearest_llm": excerpt(nearest, 300),
                "cosine": round(best, 3),
                "edit_dist": round(edit, 3)
            })
            if best >= sim_thresh:
                high_tokens += len(fseg.split())

    mean_sim = 0.0 if not rows else round(sum(r["cosine"] for r in rows) / len(rows), 3)
    high_share = round(high_tokens / max(1, total_tokens), 3)
    return {"backend": SIM_BACKEND, "mean": mean_sim, "high_share": high_share, "rows": rows[:40]}


# ----------------------------
# METRICS (process indicators)
# ----------------------------
def compute_process_metrics(events: List[Dict]) -> Dict:
    ev = [e for e in events if e.get("assignment_id") == ASSIGNMENT["id"]]
    prompts = [e for e in ev if e.get("event_type") == "prompt"]
    edits = [e for e in ev if e.get("event_type") == "edit"]
    submits = [e for e in ev if e.get("event_type") == "milestone_submit"]
    reflections = [e for e in ev if e.get("event_type") == "reflection"]

    intent_mix = {}
    for p in prompts:
        i = p.get("intent","Other") or "Other"
        intent_mix[i] = intent_mix.get(i, 0) + 1

    iteration_depth = len(edits) + len(set([p.get("intent","Other") for p in prompts]))
    revision_chars = sum(len((e.get("response") or "")) for e in edits)
    total_out = sum((e.get("response_len") or 0) if isinstance(e.get("response_len"), int) else len(e.get("response") or "") for e in ev)
    revision_ratio = round((revision_chars / max(1, total_out)), 3)

    return {
        "iteration_depth": iteration_depth,
        "num_prompts": len(prompts),
        "num_edits": len(edits),
        "milestones_completed": len(submits),
        "reflections": len(reflections),
        "revision_ratio": revision_ratio,
        "intent_mix": intent_mix
    }

# ----------------------------
# UI LAYOUT
# ----------------------------
st.title("LLM Assessment — Prototype")

role = st.sidebar.selectbox("Role", ["Student", "Instructor (prototype)"])
st.sidebar.caption(f"User ID: `{st.session_state.user_id}`")
if st.sidebar.button("🔧 Sheets connectivity test", use_container_width=True):
    ok, info = run_connectivity_test()
    st.sidebar.success(f"Write OK @ {info}" if ok else f"Failed: {info}")

st.sidebar.markdown("---")
st.sidebar.write("Similarity backend:", SIM_BACKEND)

# ----------------------------
# STUDENT VIEW
# ----------------------------
def milestone_header():
    m = ASSIGNMENT["milestones"][st.session_state.milestone_index]
    st.subheader(f"{ASSIGNMENT['title']} — {m['id']} · {m['name']}")
    st.progress((st.session_state.milestone_index + 1) / len(ASSIGNMENT["milestones"]))
    with st.expander("Guidance", expanded=False):
        st.markdown(
            "- Use the assistant to **understand, plan, critique, and refine**.\n"
            "- Keep brief **notes** on what you accepted/rejected and why.\n"
            "- You’ll generate an **Evidence Pack** (JSON) when finished."
        )

def student_view():
    milestone_header()
    m = ASSIGNMENT["milestones"][st.session_state.milestone_index]

    # Reflection note
    if m["require_note"]:
        note = st.text_area("Brief note for this step (1–3 sentences):", key=f"note_{m['id']}")
        if note:
            emit_event({
                "timestamp": datetime.datetime.now().isoformat(),
                "user_id": st.session_state.user_id,
                "assignment_id": ASSIGNMENT["id"],
                "turn_count": st.session_state.turn_count,
                "event_type": "reflection",
                "milestone_id": m["id"],
                "intent": "",
                "attachment_type": "text",
                "prompt": "",
                "response": note,
                "latency_ms": 0,
                "flags": "",
                "ui_context": "reflection",
            })

    # Chat
    user_prompt = st.chat_input("Ask for feedback, clarification, or suggestions for this milestone…")
    if user_prompt:
        st.session_state.turn_count += 1
        intent = classify_intent(user_prompt)
        emit_event({
            "timestamp": datetime.datetime.now().isoformat(),
            "user_id": st.session_state.user_id,
            "assignment_id": ASSIGNMENT["id"],
            "turn_count": st.session_state.turn_count,
            "event_type": "prompt",
            "milestone_id": m["id"],
            "intent": intent,
            "attachment_type": "text",
            "prompt": user_prompt if STORE_FULL_TEXT else excerpt(user_prompt),
            "response": "",
            "latency_ms": 0,
            "flags": "",
            "ui_context": "chat",
        })

        with st.container():
            st.markdown(f'<div class="chat-msg chat-user">{user_prompt}</div>', unsafe_allow_html=True)
            reply, latency_ms = ask_gemini(user_prompt)
            st.markdown(f'<div class="chat-msg chat-assistant">{reply}</div>', unsafe_allow_html=True)
            st_copy_to_clipboard(reply, "Copy response")

        # Log LLM output and store artifact
        emit_event({
            "timestamp": datetime.datetime.now().isoformat(),
            "user_id": st.session_state.user_id,
            "assignment_id": ASSIGNMENT["id"],
            "turn_count": st.session_state.turn_count,
            "event_type": "llm_response",
            "milestone_id": m["id"],
            "intent": intent,
            "attachment_type": "text",
            "prompt": user_prompt if STORE_FULL_TEXT else excerpt(user_prompt),
            "response": reply if STORE_FULL_TEXT else excerpt(reply),
            "latency_ms": latency_ms,
            "flags": "",
            "ui_context": "chat",
        })
        store_artifact("llm_output", reply, segmented=False)

    # Draft area
    draft = st.text_area("Working draft for this milestone:", height=220, key=f"draft_{m['id']}")
    cols = st.columns(3)
    with cols[0]:
        if st.button("Save draft snapshot"):
            # basic sudden-jump flag
            prev_len = st.session_state.last_draft_len.get(m["id"], 0)
            cur_len = len(draft or "")
            flag = "sudden_jump" if (cur_len - prev_len) > 800 else ""
            st.session_state.last_draft_len[m["id"]] = cur_len

            emit_event({
                "timestamp": datetime.datetime.now().isoformat(),
                "user_id": st.session_state.user_id,
                "assignment_id": ASSIGNMENT["id"],
                "turn_count": st.session_state.turn_count,
                "event_type": "edit",
                "milestone_id": m["id"],
                "intent": "",
                "attachment_type": "text",
                "prompt": "",
                "response": draft if STORE_FULL_TEXT else excerpt(draft),
                "latency_ms": 0,
                "flags": flag,
                "ui_context": "draft",
            })
            store_artifact("draft", draft, segmented=False)
            st.success("Snapshot saved.")

    with cols[1]:
        if st.button("⬅️ Previous", disabled=st.session_state.milestone_index == 0):
            if st.session_state.milestone_index > 0:
                st.session_state.milestone_index -= 1
                st.experimental_rerun()

    with cols[2]:
        if st.button("Mark milestone complete ✅"):
            emit_event({
                "timestamp": datetime.datetime.now().isoformat(),
                "user_id": st.session_state.user_id,
                "assignment_id": ASSIGNMENT["id"],
                "turn_count": st.session_state.turn_count,
                "event_type": "milestone_submit",
                "milestone_id": m["id"],
                "intent": "",
                "attachment_type": "",
                "prompt": "",
                "response": "",
                "latency_ms": 0,
                "flags": "",
                "ui_context": "milestone",
            })
            if st.session_state.milestone_index < len(ASSIGNMENT["milestones"]) - 1:
                st.session_state.milestone_index += 1
                st.experimental_rerun()

    st.divider()
    st.markdown("### Final submission (for voice report)")
    final_text = st.text_area("Paste your final coursework text here (prototype):", height=220, key="final_text_area")

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Generate Evidence Pack (JSON)"):
            st.session_state.evidence_json = build_evidence_pack()
            st.success("Evidence Pack generated — see sidebar to download.")

    with c2:
        if st.button("Save final as artefact"):
            if final_text.strip():
                store_artifact("final_submission", final_text, segmented=True)
                st.success("Final submission stored.")
            else:
                st.warning("Final text is empty.")

    with c3:
        if st.button("Run Voice Report"):
            if not final_text.strip():
                st.warning("Please paste your final text first.")
            else:
                # gather all LLM outputs from this session (cache only; lightweight)
                llm_texts = [a["text"] for a in st.session_state.artifacts_cache if a["kind"] == "llm_output"]
                report = compute_similarity_report(final_text, llm_texts, sim_thresh=SIM_THRESHOLD)
                st.success(f"Similarity backend: {report['backend']}")
                st.write(f"**Mean similarity**: {report['mean']}  |  **High-sim share** (≥{SIM_THRESHOLD}): {report['high_share']*100:.1f}%")
                with st.expander("Top matches (trimmed)", expanded=False):
                    for r in report["rows"]:
                        st.markdown(f"- **Cosine:** {r['cosine']}  | **Edit-dist:** {r['edit_dist']}")
                        st.markdown(f"  - Final: {r['final_seg']}")
                        st.markdown(f"  - LLM : {r['nearest_llm']}")

# ----------------------------
# INSTRUCTOR VIEW (prototype)
# ----------------------------
def read_all_events() -> List[Dict]:
    vals = EVENTS_WS.get_all_values()
    if not vals or len(vals) <= 1: return []
    hdr = vals[0]; rows = vals[1:]
    out = []
    for r in rows:
        obj = {hdr[i]: (r[i] if i < len(r) else "") for i in range(len(hdr))}
        # coerce some
        try:
            obj["response_len"] = int(obj.get("response_len","0") or 0)
        except Exception:
            pass
        out.append(obj)
    return out

def instructor_view():
    st.subheader("Instructor dashboard (prototype)")
    st.caption("Shows basic cohort/process metrics using Google Sheets data (events worksheet).")
    if st.button("Refresh data"):
        st.experimental_rerun()

    events = read_all_events()
    st.write(f"Total events: {len(events)}")

    # Simple filters
    user_filter = st.text_input("Filter by user_id (optional)")
    if user_filter.strip():
        events = [e for e in events if e.get("user_id") == user_filter.strip()]
    st.write(f"Events after filter: {len(events)}")

    # Process metrics (per user)
    users = sorted(set([e.get("user_id") for e in events]))
    cols = st.columns(2)
    with cols[0]:
        st.markdown("**Users**")
        st.write(users[:100])

    # Aggregate intent mix
    intent_counts = {}
    for e in events:
        if e.get("event_type") == "prompt":
            lab = e.get("intent","Other") or "Other"
            intent_counts[lab] = intent_counts.get(lab, 0) + 1
    with cols[1]:
        st.markdown("**Intent mix (counts)**")
        st.write(intent_counts)

    # Sample: compute metrics for a single user (first or filtered)
    if users:
        u0 = st.selectbox("Inspect user", users)
        ev_u = [e for e in events if e.get("user_id") == u0]
        metrics = compute_process_metrics(ev_u)
        st.markdown("### Process metrics (selected user)")
        st.json(metrics)

    with st.expander("Recent events (tail)", expanded=False):
        tail = events[-50:] if len(events) > 50 else events
        for e in tail:
            ts = e.get("timestamp","")
            et = e.get("event_type","")
            ms = e.get("milestone_id","")
            intent = e.get("intent","")
            st.markdown(f"- `{ts}` | **{et}** @ {ms} | intent: *{intent}* | resp_len: {e.get('response_len')} | flags: {e.get('flags')}")

# ----------------------------
# ROUTER
# ----------------------------
if role == "Student":
    student_view()
else:
    instructor_view()

# ----------------------------
# SIDEBAR DOWNLOAD
# ----------------------------
if st.session_state.evidence_json:
    st.sidebar.download_button(
        "📥 Download Evidence Pack (JSON)",
        data=st.session_state.evidence_json,
        file_name=f"evidence_{ASSIGNMENT['id']}_{st.session_state.user_id}.json",
        mime="application/json",
        use_container_width=True,
    )
