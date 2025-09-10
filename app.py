# --- New: config & models
ASSIGNMENT = {
    "id": "AFM_2025_CW1",
    "title": "Investment Appraisal Brief",
    "milestones": [
        {"id": "M0", "name": "Plan", "require_note": True},
        {"id": "M1", "name": "Concept Checks", "require_note": True},
        {"id": "M2", "name": "Outline (CEE frame)", "require_note": True},
        {"id": "M3", "name": "First Draft", "require_note": False},
        {"id": "M4", "name": "Critical Revisions", "require_note": True},
        {"id": "M5", "name": "References & Integrity", "require_note": True},
        {"id": "M6", "name": "Final Synthesis", "require_note": True},
    ],
}

# --- Session init additions
if "milestone_index" not in st.session_state:
    st.session_state.milestone_index = 0
if "events" not in st.session_state:
    st.session_state.events = []  # local cache for Evidence Pack

def emit_event(event):
    # Minimal JSON event; also append to Google Sheet for now
    st.session_state.events.append(event)
    row = [
        datetime.datetime.now().isoformat(),
        st.session_state.anonymized_user_id,
        ASSIGNMENT["id"],
        event.get("event_type",""),
        event.get("milestone_id",""),
        len(event.get("text","") or ""),
        event.get("token_in", 0),
        event.get("token_out", 0),
        event.get("latency_ms", 0),
        event.get("flags",""),
    ]
    try:
        gsheet.append_row(row)
    except Exception as e:
        st.warning(f"Log failed: {e}")

def milestone_header():
    m = ASSIGNMENT["milestones"][st.session_state.milestone_index]
    st.subheader(f"{m['id']} • {m['name']}")
    st.progress((st.session_state.milestone_index+1)/len(ASSIGNMENT["milestones"]))

# --- Chat handler (wrap)
def ask_llm(prompt):
    start = time.time()
    chunks, text = [], ""
    for c in gemini_model.generate_content([prompt], stream=True):
        chunks.append(c.text or "")
    text = "".join(chunks)
    latency_ms = round((time.time()-start)*1000)
    emit_event({
        "timestamp": datetime.datetime.now().isoformat(),
        "event_type": "llm_response",
        "milestone_id": ASSIGNMENT["milestones"][st.session_state.milestone_index]["id"],
        "text": text,
        "token_in": len(prompt),
        "token_out": len(text),
        "latency_ms": latency_ms,
    })
    return text

# --- UI: milestone scaffold
def show_assessment_mode():
    st.title(f"{ASSIGNMENT['title']} — Assessment Mode")
    milestone_header()

    # Student reflection (short rationale)
    m = ASSIGNMENT["milestones"][st.session_state.milestone_index]
    if m["require_note"]:
        note = st.text_area("Briefly explain what you intend to do in this step (1–3 sentences):")
        if note:
            emit_event({
                "timestamp": datetime.datetime.now().isoformat(),
                "event_type": "reflection",
                "milestone_id": m["id"],
                "text": note
            })

    # Chat box
    user_prompt = st.chat_input("Ask the assistant or request feedback on this milestone…")
    if user_prompt:
        emit_event({
            "timestamp": datetime.datetime.now().isoformat(),
            "event_type": "prompt",
            "milestone_id": m["id"],
            "text": user_prompt
        })
        reply = ask_llm(user_prompt)
        st.write(reply)

    # Draft workspace
    draft = st.text_area("Your working draft for this milestone:", height=220, key=f"draft_{m['id']}")
    if st.button("Save draft snapshot"):
        emit_event({
            "timestamp": datetime.datetime.now().isoformat(),
            "event_type": "edit",
            "milestone_id": m["id"],
            "text": draft
        })
        st.success("Snapshot saved.")

    cols = st.columns(3)
    with cols[0]:
        if st.button("⬅️ Previous", disabled=st.session_state.milestone_index==0):
            st.session_state.milestone_index -= 1
            st.rerun()
    with cols[1]:
        if st.button("Mark milestone complete ✅"):
            emit_event({
                "timestamp": datetime.datetime.now().isoformat(),
                "event_type": "milestone_submit",
                "milestone_id": m["id"]
            })
            if st.session_state.milestone_index < len(ASSIGNMENT["milestones"])-1:
                st.session_state.milestone_index += 1
                st.rerun()
    with cols[2]:
        if st.button("Generate Evidence Pack"):
            pack = build_evidence_pack(st.session_state.events, st.session_state.anonymized_user_id, ASSIGNMENT)
            st.download_button("📥 Download Evidence Pack (JSON)", data=pack, file_name=f"evidence_{ASSIGNMENT['id']}_{st.session_state.anonymized_user_id}.json")
            
def build_evidence_pack(events, user_id, assignment):
    import json, hashlib
    payload = {
        "version": "1.0",
        "assignment": assignment,
        "student": {"pseudonymous_id": user_id},
        "events": events,
        "created_at": datetime.datetime.now().isoformat(),
    }
    b = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    digest = hashlib.sha256(b).hexdigest()
    payload["sha256"] = digest
    return json.dumps(payload, ensure_ascii=False, indent=2)

# Router
show_assessment_mode()
