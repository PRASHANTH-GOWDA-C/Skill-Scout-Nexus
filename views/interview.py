"""Mock Interviews: Proctored Written Interview (Typing) & Proctored Hands-Free Voice
Interview side by side, with strict camera/mic/fullscreen proctoring, next-question navigation,
restart, and exit controls."""

from __future__ import annotations

import base64
import html
import json
import uuid
from functools import partial

import streamlit as st
import streamlit.components.v1 as components

from core import proctor_guard, speech_router
from piq import agents, formatter, journey, services
from piq.config import BROWSER_VOICE_LANG, INTERVIEW_MAX_TURNS, INTERVIEW_TYPES, VOICE_RUBRIC_MAX
from piq.ui import Stat, page_header, require_profile, run_agent, show_last_run, stat_row

INTERVIEWER, STUDENT, FEEDBACK = (
    ":material/record_voice_over:",
    ":material/person:",
    ":material/grading:",
)
TYPE_HINTS = {
    "HR": "Motivation, teamwork and how you handle situations, told with examples.",
    "Technical": "Core concepts for your target role, explained in your own words.",
    "Project": "The projects on your resume: design choices, problems and results.",
}
VOICE_CAT_LABELS = {
    "technical": "Technical",
    "situational": "Situational",
    "behavioral": "Behavioural",
}


def _exit_interview() -> None:
    st.session_state.pop("interview", None)
    st.session_state.pop("voice_convo", None)
    st.session_state.pop("voice_answer_box", None)
    for k in [k for k in st.session_state if str(k).startswith("voice_answer_")]:
        st.session_state.pop(k, None)
    st.session_state["assessment_active"] = False
    proctor_guard.reset_proctor_state()
    proctor_guard.clear_proctor_overlay()


def _hands_free_voice_html(
    question_text: str,
    audio_bytes: bytes | None,
    mime_type: str,
    turn_id: str,
) -> str:
    """Continuous hands-free AI tell -> continuous SpeechRecognition -> 5s quiet countdown -> auto-submit."""
    q_js = json.dumps(question_text).replace("<", "\\u003c").replace(">", "\\u003e")
    b64 = base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else ""
    mime_js = json.dumps(mime_type or "audio/mpeg")
    tid_js = json.dumps(turn_id)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ margin:0; padding:10px 12px; font:14px/1.45 system-ui,-apple-system,sans-serif; background:#111a1c; color:#e3eae8; border-radius:10px; border:1px solid #1f7f86; }}
  .row {{ display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap; }}
  .pill {{ font-weight:700; color:#6ccbc4; font-size:13px; }}
  .controls {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; }}
  .transcript {{ margin-top:8px; padding:8px 10px; border-radius:8px; background:#1a2528; color:#cbd5d3; min-height:46px; max-height:92px; overflow-y:auto; font-size:13px; }}
  .countdown {{ margin-top:6px; font-size:12px; font-weight:600; color:#f6c453; min-height:18px; }}
  button {{ background:#0d6670; color:#fff; border:0; border-radius:6px; padding:6px 11px; font-weight:600; cursor:pointer; font-size:12px; }}
  button.sec {{ background:#1f3237; color:#d7e3e0; border:1px solid #35545c; }}
  button.warn {{ background:#7c4a03; color:#fde68a; border:1px solid #b45309; }}
  button:disabled {{ opacity:0.55; cursor:not-allowed; }}
</style>
</head>
<body>
<div class="row">
  <span id="hf-status" class="pill">🔊 AI Interviewer speaking…</span>
  <div class="controls" id="hf-controls" style="display:none;">
    <button type="button" id="hf-continue" class="sec">🎙️ Continue Speaking</button>
    <button type="button" id="hf-hold" class="sec">⏸️ Hold Auto-Send</button>
    <button type="button" id="hf-clear" class="sec">🔄 Clear</button>
    <button type="button" id="hf-send-now">✅ Finish &amp; Send Answer</button>
  </div>
</div>
<div id="hf-transcript" class="transcript">Waiting for interviewer to finish speaking…</div>
<div id="hf-countdown" class="countdown"></div>
<script>
(function() {{
  const qText = {q_js};
  const b64 = "{b64}";
  const mime = {mime_js};
  const turnId = {tid_js};
  const AUTO_SEND_SECS = 5;
  const statusEl = document.getElementById("hf-status");
  const transEl = document.getElementById("hf-transcript");
  const countEl = document.getElementById("hf-countdown");
  const ctrlEl = document.getElementById("hf-controls");
  const contBtn = document.getElementById("hf-continue");
  const holdBtn = document.getElementById("hf-hold");
  const clearBtn = document.getElementById("hf-clear");
  const sendNowBtn = document.getElementById("hf-send-now");
  const parentWin = window.parent || window;
  const parentDoc = parentWin.document || document;

  if (parentWin.__piqVoiceTurn === turnId) return;
  parentWin.__piqVoiceTurn = turnId;
  parentWin.__piqAiSpeaking = true;

  let committedText = "";
  let sessionFinal = "";
  let latestInterim = "";
  let silenceTimeout = null;
  let countdownInterval = null;
  let secsLeft = 0;
  let recog = null;
  let listeningActive = false;
  let autoSendHold = false;
  let submitted = false;
  let lastSpeechAt = 0;

  function fullTranscript() {{
    return [committedText, sessionFinal, latestInterim].filter(Boolean).join(" ").replace(/\\s+/g, " ").trim();
  }}

  function syncParentTextarea(text) {{
    try {{
      const areas = parentDoc.querySelectorAll("textarea");
      let target = null;
      for (const a of areas) {{
        const lbl = (a.getAttribute("aria-label") || "").toLowerCase();
        if (lbl.includes("spoken") || lbl.includes("answer")) {{ target = a; break; }}
      }}
      if (!target && areas.length) target = areas[areas.length - 1];
      if (target) {{
        const setter = Object.getOwnPropertyDescriptor(parentWin.HTMLTextAreaElement.prototype, "value").set;
        setter.call(target, text);
        target.dispatchEvent(new parentWin.Event("input", {{ bubbles: true }}));
        target.dispatchEvent(new parentWin.Event("change", {{ bubbles: true }}));
      }}
    }} catch (e) {{}}
  }}

  function commitParentTextarea(text) {{
    try {{
      const areas = parentDoc.querySelectorAll("textarea");
      let target = null;
      for (const a of areas) {{
        const lbl = (a.getAttribute("aria-label") || "").toLowerCase();
        if (lbl.includes("spoken") || lbl.includes("answer")) {{ target = a; break; }}
      }}
      if (!target && areas.length) target = areas[areas.length - 1];
      if (target) {{
        const setter = Object.getOwnPropertyDescriptor(parentWin.HTMLTextAreaElement.prototype, "value").set;
        setter.call(target, text);
        target.dispatchEvent(new parentWin.Event("input", {{ bubbles: true }}));
        target.dispatchEvent(new parentWin.Event("change", {{ bubbles: true }}));
        target.dispatchEvent(new parentWin.FocusEvent("blur", {{ bubbles: false }}));
      }}
    }} catch (e) {{}}
  }}

  function stopCountdown() {{
    clearTimeout(silenceTimeout);
    clearInterval(countdownInterval);
    silenceTimeout = null;
    countdownInterval = null;
    secsLeft = 0;
    if (countEl && !submitted) {{
      countEl.textContent = autoSendHold
        ? "⏸️ Auto-send paused — click 'Finish & Send Answer' or resume auto-send when ready."
        : "";
    }}
  }}

  function scheduleAutoSubmit() {{
    stopCountdown();
    if (submitted || autoSendHold) return;
    const text = fullTranscript();
    const words = text ? text.split(/\\s+/).filter(Boolean).length : 0;
    if (words < 3) return;
    secsLeft = AUTO_SEND_SECS;
    countEl.textContent = "⏱️ Quiet detected — sending answer automatically in " + secsLeft + "s (click 'Continue Speaking' to keep talking)…";
    countdownInterval = setInterval(() => {{
      if (submitted || autoSendHold) {{ stopCountdown(); return; }}
      secsLeft -= 1;
      if (secsLeft > 0) {{
        countEl.textContent = "⏱️ Quiet detected — sending answer automatically in " + secsLeft + "s (click 'Continue Speaking' to keep talking)…";
      }} else {{
        stopCountdown();
        submitParentAnswer();
      }}
    }}, 1000);
  }}

  function submitParentAnswer() {{
    if (submitted) return;
    const cleaned = fullTranscript();
    if (!cleaned) {{
      countEl.textContent = "Speak or type your answer first before submitting.";
      return;
    }}
    submitted = true;
    listeningActive = false;
    stopCountdown();
    parentWin.__piqAiSpeaking = false;
    try {{ if (recog) recog.stop(); }} catch (e) {{}}
    sendNowBtn.disabled = true;
    contBtn.disabled = true;
    holdBtn.disabled = true;
    clearBtn.disabled = true;
    commitParentTextarea(cleaned);
    statusEl.textContent = "⏳ Submitting spoken answer for evaluation…";
    countEl.textContent = "Evaluating your response and preparing the next question…";
    setTimeout(() => {{
      try {{
        const btns = parentDoc.querySelectorAll("button");
        for (const b of btns) {{
          if ((b.innerText || "").includes("Submit spoken answer")) {{
            b.click();
            return;
          }}
        }}
      }} catch (e) {{}}
    }}, 160);
  }}

  sendNowBtn.onclick = submitParentAnswer;

  contBtn.onclick = function() {{
    if (submitted) return;
    stopCountdown();
    lastSpeechAt = Date.now();
    statusEl.textContent = "🎙️ Listening extended — keep speaking freely!";
    countEl.textContent = "Timer reset. Speak as long as you need; auto-sends 5s after you pause.";
    if (!listeningActive) {{
      startHandsFreeListening();
    }} else if (recog) {{
      try {{ recog.start(); }} catch (e) {{}}
    }}
  }};

  holdBtn.onclick = function() {{
    if (submitted) return;
    autoSendHold = !autoSendHold;
    if (autoSendHold) {{
      holdBtn.textContent = "▶️ Resume 5s Auto-Send";
      holdBtn.className = "warn";
      stopCountdown();
    }} else {{
      holdBtn.textContent = "⏸️ Hold Auto-Send";
      holdBtn.className = "sec";
      countEl.textContent = "5s silence auto-send active.";
      if (fullTranscript().split(/\\s+/).filter(Boolean).length >= 3) {{
        scheduleAutoSubmit();
      }}
    }}
  }};

  clearBtn.onclick = function() {{
    if (submitted) return;
    committedText = "";
    sessionFinal = "";
    latestInterim = "";
    stopCountdown();
    syncParentTextarea("");
    transEl.textContent = "Listening…";
    statusEl.textContent = "🎙️ Cleared — listening for your answer…";
  }};

  function createRecognizer() {{
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition || parentWin.SpeechRecognition || parentWin.webkitSpeechRecognition;
    if (!SR) return null;
    const r = new SR();
    r.lang = "{BROWSER_VOICE_LANG}";
    r.continuous = true;
    r.interimResults = true;
    r.onresult = (event) => {{
      if (submitted) return;
      let interim = "";
      let accrued = "";
      for (let i = 0; i < event.results.length; i++) {{
        const seg = event.results[i][0].transcript;
        if (event.results[i].isFinal) accrued += seg + " ";
        else interim += seg;
      }}
      sessionFinal = accrued.trim();
      latestInterim = interim.trim();
      const combined = fullTranscript();
      transEl.textContent = combined || "Listening…";
      syncParentTextarea(combined);
      lastSpeechAt = Date.now();
      stopCountdown();
      // Only start 5s silence countdown after a brief quiet pause (when not actively speaking)
      if (!autoSendHold && combined.split(/\\s+/).filter(Boolean).length >= 3) {{
        silenceTimeout = setTimeout(scheduleAutoSubmit, 1200);
      }}
    }};
    r.onerror = (ev) => {{
      if (submitted) return;
      const err = ev && ev.error ? ev.error : "";
      if (err === "not-allowed" || err === "service-not-allowed") {{
        listeningActive = false;
        statusEl.textContent = "🎙️ Microphone blocked — enable mic or type below.";
      }}
    }};
    r.onend = () => {{
      if (sessionFinal) {{
        committedText = [committedText, sessionFinal].filter(Boolean).join(" ").trim();
        sessionFinal = "";
      }}
      latestInterim = "";
      if (listeningActive && !submitted) {{
        // Browser paused SpeechRecognition after ~15-60s: restart seamlessly so listening never stops until quiet!
        setTimeout(() => {{
          if (!listeningActive || submitted) return;
          try {{
            recog = createRecognizer();
            if (recog) recog.start();
          }} catch (e) {{}}
        }}, 150);
      }}
    }};
    return r;
  }}

  function startHandsFreeListening() {{
    parentWin.__piqAiSpeaking = false;
    ctrlEl.style.display = "flex";
    const r = createRecognizer();
    if (!r) {{
      statusEl.textContent = "🎙️ Speech recognition unavailable here — type your answer below.";
      transEl.textContent = "Your browser does not support automatic SpeechRecognition; type below and click Submit.";
      return;
    }}
    listeningActive = true;
    recog = r;
    statusEl.textContent = "🎙️ Listening continuously… Speaks until you pause 5s or click Finish";
    if (!fullTranscript()) transEl.textContent = "Listening…";
    try {{ recog.start(); }} catch (e) {{}}
  }}

  function speakQuestion() {{
    parentWin.__piqAiSpeaking = true;
    if (b64) {{
      const aud = new Audio("data:" + mime + ";base64," + b64);
      aud.onended = startHandsFreeListening;
      aud.onerror = () => speakWithWebSpeech();
      aud.play().catch(() => speakWithWebSpeech());
    }} else {{
      speakWithWebSpeech();
    }}
  }}

  function speakWithWebSpeech() {{
    if (!("speechSynthesis" in window)) {{
      startHandsFreeListening();
      return;
    }}
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(qText);
    u.lang = "{BROWSER_VOICE_LANG}";
    u.rate = 1.0;
    u.onend = startHandsFreeListening;
    u.onerror = startHandsFreeListening;
    window.speechSynthesis.speak(u);
  }}

  // Wait briefly for fullscreen/media unlock if overlay is visible
  function startWhenUnlocked() {{
    const ov = parentDoc.getElementById("piq-proctor-lock-overlay");
    if (ov && ov.style.display !== "none") {{
      setTimeout(startWhenUnlocked, 350);
      return;
    }}
    speakQuestion();
  }}
  startWhenUnlocked();
}})();
</script>
</body>
</html>"""


def _start_voice_convo(sid: str, stream: str, category: str, use_viva: bool) -> dict:
    """Create a multi-turn hands-free Voice Conversation session."""
    profile = services.current_profile(sid)
    viva_list = list(st.session_state.get("github_viva_questions", [])) if use_viva else []
    first_custom = viva_list.pop(0) if viva_list else None
    vq = journey.voice_question(sid, stream, category, custom_question=first_custom)
    return {
        "session_id": uuid.uuid4().hex,
        "stream": stream,
        "category": category,
        "use_viva": use_viva,
        "remaining_viva": viva_list,
        "history": [],
        "current_vq": vq,
        "turn_idx": 1,
        "max_turns": INTERVIEW_MAX_TURNS,
        "done": False,
        "last_submitted_turn": None,
        "profile_name": profile.name if profile else "Candidate",
    }


def _next_voice_turn(sid: str, convo: dict) -> dict:
    """Generate the next distinct conversational follow-up question and synthesize audio."""
    if len(convo["history"]) >= convo["max_turns"] or convo["turn_idx"] >= convo["max_turns"]:
        convo["done"] = True
        return convo
    profile = services.current_profile(sid)
    qa = [{"question": h["question"], "answer": h["answer"]} for h in convo["history"]]
    asked = [h["question"] for h in convo["history"]]
    cur_vq = convo.get("current_vq")
    if cur_vq is not None and getattr(cur_vq, "question", None) is not None:
        asked.append(cur_vq.question.question_text)
    asked = list(dict.fromkeys(asked))

    remaining_viva = list(convo.get("remaining_viva", []))
    if remaining_viva:
        custom_q = remaining_viva.pop(0)
        convo["remaining_viva"] = remaining_viva
        next_vq = journey.voice_question(
            sid, convo["stream"], convo["category"], custom_question=custom_q
        )
    elif profile is not None:
        try:
            tracks = services.interview_tracks(profile)
            track_focus = next(
                (t.focus for t in tracks if t.focus),
                f"Conversational {convo['category']} voice interview",
            )
            turn = agents.next_question(
                profile,
                convo["category"].capitalize(),
                qa,
                [{"question": q, "answer": ""} for q in asked if q not in {h["question"] for h in convo["history"]}],
                sid,
                focus=f"{track_focus} ({convo['category']})",
            )
            from agents.voice_interview_agent import VoiceInterviewQuestion

            q_obj = VoiceInterviewQuestion(
                category=convo["category"],
                stream=convo["stream"],
                role=profile.target_role,
                question_text=turn.question,
                intent=turn.intent,
                scenario_context="Conversational Follow-Up" if turn.is_followup else "Domain Deep Dive",
            )
            speech = speech_router.synthesize(q_obj.question_text)
            next_vq = journey.VoiceQuestion(q_obj, speech)
        except Exception:
            next_vq = journey.voice_question(
                sid, convo["stream"], convo["category"], exclude_questions=asked
            )
    else:
        next_vq = journey.voice_question(
            sid, convo["stream"], convo["category"], exclude_questions=asked
        )

    convo["current_vq"] = next_vq
    convo["turn_idx"] = len(convo["history"]) + 1
    return convo


def _setup(sid: str) -> None:
    st.session_state["assessment_active"] = False
    proctor_guard.clear_proctor_overlay()
    me = journey.student(sid)
    st.caption(
        "Choose your preferred mock interview mode below. Both modes enforce strict proctoring "
        "(Camera, Microphone, Fullscreen, and No Tab/App Switching)."
    )

    typing_col, voice_col = st.columns(2, gap="large")

    # Left: Typing Mock Interview
    with typing_col, st.container(border=True, height="stretch"):
        st.subheader(":material/keyboard: Proctored Written Interview", anchor=False)
        st.caption(
            f"Type detailed responses across up to {INTERVIEW_MAX_TURNS} critic-checked questions "
            "with adaptive follow-ups and 4-criterion rubric scoring."
        )
        profile = services.current_profile(sid)
        tracks = services.interview_tracks(profile)
        track_names = [t.name for t in tracks]
        track_hints = {t.name: t.focus or TYPE_HINTS.get(t.name, "") for t in tracks}
        kind = st.selectbox(
            "Interview focus / domain track",
            track_names,
            index=0,
            key="typing_kind_select",
            help="Includes specialized tracks for your career domain as well as HR, Technical, and Project.",
        )
        if kind and track_hints.get(kind):
            st.caption(f"Focus: {formatter.md_escape(track_hints[kind])}")
        if st.button(
            "Start Written Interview",
            type="primary",
            icon=":material/play_arrow:",
            key="start_typing_btn",
            width="stretch",
        ):
            proctor_guard.reset_proctor_state()
            session = run_agent(
                "Interview agent", lambda ev: services.start_interview(sid, kind, ev)
            )
            if session:
                st.session_state["interview"] = session
                st.session_state["assessment_active"] = True
                st.rerun()
        show_last_run("Interview agent")

    # Right: Hands-Free Voice Interview
    with voice_col, st.container(border=True, height="stretch"):
        st.subheader(":material/graphic_eq: Proctored Hands-Free Voice Interview", anchor=False)
        st.caption(
            f"Full hands-free AI conversation ({INTERVIEW_MAX_TURNS} questions): the AI speaks "
            "automatically, listens to your microphone automatically, evaluates each answer, and "
            "asks dynamic follow-up questions."
        )
        stream = journey.voice_stream(me.stream)
        category = st.segmented_control(
            "Conversation style",
            journey.CATEGORIES,
            default="situational",
            format_func=lambda c: VOICE_CAT_LABELS[c],
            key="voice_cat_seg",
        )
        viva_list = st.session_state.get("github_viva_questions", [])
        use_viva = False
        if viva_list:
            use_viva = st.toggle(
                ":material/hub: Include GitHub Project Viva questions",
                value=True,
                key="voice_use_viva",
                help="Includes architecture questions from your imported GitHub repository.",
            )
        if st.button(
            "Start Hands-Free Voice Interview",
            type="primary",
            icon=":material/mic:",
            key="start_voice_btn",
            width="stretch",
        ):
            proctor_guard.reset_proctor_state()
            convo = _start_voice_convo(sid, stream, category or "situational", use_viva)
            st.session_state["voice_convo"] = convo
            st.session_state["voice_answer_box"] = ""
            st.session_state["assessment_active"] = True
            st.rerun()


def _feedback(turn: services.AnsweredTurn, latest: bool) -> None:
    with st.chat_message("Feedback", avatar=FEEDBACK):
        st.markdown(formatter.feedback_title(turn.evaluation))
        st.html(formatter.criteria_html(turn.evaluation.scores))
        with st.expander("Strengths, what to improve and a model answer", expanded=latest):
            st.markdown(formatter.evaluation_md(turn.evaluation))


def _run_typing_interview(sid: str, session: services.InterviewSession) -> None:
    if not session.done:
        st.session_state["assessment_active"] = True
        proctor_guard.render_proctor_guard(page_type="interview")
        proctor_guard.render_warning_banner()
        if proctor_guard.is_breached():
            session = session.model_copy(update={"done": True, "current": None})
            st.session_state["interview"] = session
            st.session_state["assessment_active"] = False
            st.rerun()
    else:
        st.session_state["assessment_active"] = False
        proctor_guard.clear_proctor_overlay()

    value, words = formatter.interview_progress(
        len(session.history), session.current is not None, INTERVIEW_MAX_TURNS
    )
    h_l, h_r = st.columns([3, 2], vertical_alignment="center")
    with h_l:
        st.subheader(f"{session.interview_type} written interview", anchor=False)
    with h_r, st.container(horizontal=True):
        if not session.done and st.button("Next question", icon=":material/skip_next:", key="typ_next"):
            if session.current is not None:
                # Skip current question cleanly and advance to next
                placeholder_ans = "[Skipped to next question]"
                answered = run_agent(
                    "Evaluator", lambda _: services.answer_question(sid, session, placeholder_ans)
                )
                if answered:
                    if not answered.done:
                        nxt = run_agent(
                            "Interview agent", lambda ev: services.continue_interview(sid, answered, ev)
                        )
                        st.session_state["interview"] = nxt or answered
                    else:
                        st.session_state["interview"] = answered
                    st.rerun()
            else:
                nxt = run_agent(
                    "Interview agent", lambda ev: services.continue_interview(sid, session, ev)
                )
                if nxt:
                    st.session_state["interview"] = nxt
                    st.rerun()
        if st.button("Restart interview", icon=":material/restart_alt:", key="typ_restart"):
            proctor_guard.reset_proctor_state()
            fresh = run_agent(
                "Interview agent", lambda ev: services.start_interview(sid, session.interview_type, ev)
            )
            if fresh:
                st.session_state["interview"] = fresh
                st.session_state["assessment_active"] = True
                st.rerun()
        if st.button("Exit interview", type="secondary", icon=":material/logout:", key="typ_exit"):
            _exit_interview()
            st.rerun()

    st.progress(value, text=words)

    for i, turn in enumerate(session.history):
        st.chat_message("Interviewer", avatar=INTERVIEWER).markdown(formatter.md_escape(turn.question))
        st.chat_message("You", avatar=STUDENT).markdown(formatter.md_escape(turn.answer))
        _feedback(turn, latest=i == len(session.history) - 1)

    if session.done:
        if proctor_guard.is_breached():
            flags = ", ".join(proctor_guard.get_proctor_flags()) or proctor_guard.FLAG_PROCTOR_BREACH
            st.error(
                f"🚨 **PROCTOR BREACH:** This interview was terminated after 3 proctoring violations ({flags}).",
                icon=":material/gavel:",
            )
        if session.history:
            st.success(
                f"Session complete — average {session.average:.1f}/5 over {len(session.history)} questions.",
                icon=":material/task_alt:",
            )
            summary = services.interview_summary(session)
            with st.container(border=True):
                st.subheader("Session summary", anchor=False)
                st.html(formatter.criteria_html(summary.averages))
                st.markdown(formatter.summary_md(summary.averages, summary.strongest, summary.weakest))
                st.markdown("**What to work on**")
                st.markdown(formatter.tips_md(summary.tips))
            today = formatter.local_today()
            turns = [(t.question, t.answer, t.evaluation) for t in session.history]
            st.download_button(
                "Download feedback (.md)",
                partial(
                    formatter.interview_notes, session.interview_type, turns, today, summary.tips
                ),
                file_name=f"placementiq-{session.interview_type.lower()}-interview-{today:%Y%m%d}.md",
                mime="text/markdown",
                icon=":material/download:",
                type="tertiary",
                on_click="ignore",
            )
        return

    if session.current is None:
        with st.chat_message("Interviewer", avatar=INTERVIEWER):
            show_last_run("Interview agent")
            if st.button("Retry next question", type="primary", icon=":material/refresh:"):
                nxt = run_agent(
                    "Interview agent", lambda ev: services.continue_interview(sid, session, ev)
                )
                if nxt:
                    st.session_state["interview"] = nxt
                    st.rerun()
        return

    with st.chat_message("Interviewer", avatar=INTERVIEWER):
        st.markdown(formatter.question_md(session.current))
        show_last_run("Interview agent")
    if answer := st.chat_input("Your answer"):
        st.chat_message("You", avatar=STUDENT).markdown(formatter.md_escape(answer))
        answered = run_agent("Evaluator", lambda _: services.answer_question(sid, session, answer))
        if answered is None:
            st.stop()
        st.session_state["interview"] = answered
        if not answered.done:
            nxt = run_agent(
                "Interview agent", lambda ev: services.continue_interview(sid, answered, ev)
            )
            if nxt:
                st.session_state["interview"] = nxt
        st.rerun()


def _run_voice_interview(sid: str, convo: dict) -> None:
    if not convo["done"]:
        st.session_state["assessment_active"] = True
        proctor_guard.render_proctor_guard(page_type="voice_interview")
        proctor_guard.render_warning_banner()
        if proctor_guard.is_breached():
            vq = convo["current_vq"]
            journey.assess_answer(
                sid,
                vq.question,
                "[Force-submitted due to 3 proctoring strikes / PROCTOR_BREACH]",
                proctor_flags=proctor_guard.get_proctor_flags() or [proctor_guard.FLAG_PROCTOR_BREACH],
                session_id=convo["session_id"],
            )
            convo["done"] = True
            st.session_state["assessment_active"] = False
            st.rerun()
    else:
        st.session_state["assessment_active"] = False
        proctor_guard.clear_proctor_overlay()

    h_l, h_r = st.columns([3, 2], vertical_alignment="center")
    with h_l:
        st.subheader(
            f"Hands-Free Voice Interview · Question {min(convo['turn_idx'], convo['max_turns'])} of {convo['max_turns']}",
            anchor=False,
        )
    with h_r, st.container(horizontal=True):
        if not convo["done"] and st.button("Next question", icon=":material/skip_next:", key="vf_next"):
            cur_turn_id = f"{convo['session_id']}_{convo['turn_idx']}"
            cur_ans_key = f"voice_answer_{cur_turn_id}"
            ans_now = (st.session_state.get(cur_ans_key) or "").strip() or "[Moved to next question]"
            if convo.get("last_submitted_turn") != cur_turn_id:
                res = journey.assess_answer(
                    sid,
                    convo["current_vq"].question,
                    ans_now,
                    session_id=convo["session_id"],
                )
                convo["last_submitted_turn"] = cur_turn_id
                convo["history"].append(
                    {
                        "question": convo["current_vq"].question.question_text,
                        "answer": ans_now,
                        "result": res,
                    }
                )
                _next_voice_turn(sid, convo)
            st.rerun()
        if st.button("Restart interview", icon=":material/restart_alt:", key="vf_restart"):
            for k in [k for k in st.session_state if str(k).startswith("voice_answer_")]:
                st.session_state.pop(k, None)
            proctor_guard.reset_proctor_state()
            fresh = _start_voice_convo(
                sid, convo["stream"], convo["category"], convo.get("use_viva", False)
            )
            st.session_state["voice_convo"] = fresh
            st.session_state["assessment_active"] = True
            st.rerun()
        if st.button("Exit interview", type="secondary", icon=":material/logout:", key="vf_exit"):
            _exit_interview()
            st.rerun()

    st.progress(
        len(convo["history"]) / convo["max_turns"],
        text=f"{len(convo['history'])} of {convo['max_turns']} turns completed",
    )

    # Render conversation history so far
    for idx, turn in enumerate(convo["history"], 1):
        ev = turn["result"].evaluation
        r = ev.rubric
        st.chat_message("Interviewer", avatar=INTERVIEWER).markdown(
            f"**Q{idx}.** {formatter.md_escape(turn['question'])}"
        )
        st.chat_message("You", avatar=STUDENT).markdown(formatter.md_escape(turn["answer"]))
        with st.chat_message("Feedback", avatar=FEEDBACK):
            st.markdown(
                f"**{r.total_score}/{VOICE_RUBRIC_MAX} ({ev.performance_tier})** · "
                f"Content {r.content_knowledge}/5 · Clarity {r.verbal_clarity_star}/5 · Confidence {r.confidence_demeanor}/5"
            )
            with st.expander("Detailed evaluation & model answer", expanded=(idx == len(convo["history"]))):
                st.markdown("**Strengths:** " + "; ".join(formatter.md_escape(x) for x in ev.strengths))
                st.markdown("**To improve:** " + "; ".join(formatter.md_escape(x) for x in ev.areas_for_improvement))
                st.markdown("**Model answer:** " + formatter.md_escape(ev.exemplary_model_answer))

    if convo["done"]:
        if proctor_guard.is_breached():
            st.error("🚨 **PROCTOR BREACH:** Voice interview terminated after 3 strikes.", icon=":material/gavel:")
        elif convo["history"]:
            avg = round(
                sum(h["result"].evaluation.rubric.total_score for h in convo["history"]) / len(convo["history"]),
                1,
            )
            st.success(
                f"Hands-free voice interview complete — Average score {avg}/{VOICE_RUBRIC_MAX} across "
                f"{len(convo['history'])} conversational questions.",
                icon=":material/task_alt:",
            )
        return

    # Active question turn: Auto-speak + Auto-listen hands-free!
    vq = convo["current_vq"]
    q = vq.question
    turn_id = f"{convo['session_id']}_{convo['turn_idx']}"
    ans_key = f"voice_answer_{turn_id}"
    with st.chat_message("Interviewer", avatar=INTERVIEWER):
        st.caption(
            f"{formatter.md_escape(q.stream)} · {VOICE_CAT_LABELS[q.category]} · "
            f"{formatter.md_escape(q.scenario_context)}"
        )
        st.markdown(f"#### {formatter.md_escape(q.question_text)}")
        st.markdown(f":gray[What the interviewer looks for: {formatter.md_escape(q.intent)}]")
        components.html(
            _hands_free_voice_html(
                vq.speech.text,
                vq.speech.audio,
                getattr(vq.speech, "mime_type", "audio/mpeg"),
                turn_id,
            ),
            height=165,
        )

    ans_val = st.text_area(
        "Your spoken / typed answer (auto-filled hands-free while you speak)",
        key=ans_key,
        height=120,
        placeholder="Speak naturally after the interviewer finishes—your words appear here and submit automatically after 5s of silence, or click 'Continue Speaking' / type and submit manually.",
    )
    if st.button("Submit spoken answer", type="primary", icon=":material/send:", key=f"vf_submit_{turn_id}"):
        if convo.get("last_submitted_turn") == turn_id:
            st.rerun()
        try:
            with st.spinner("Evaluating answer and preparing next question…"):
                res = journey.assess_answer(
                    sid, q, ans_val, session_id=convo["session_id"]
                )
            convo["last_submitted_turn"] = turn_id
            convo["history"].append(
                {
                    "question": q.question_text,
                    "answer": ans_val.strip(),
                    "result": res,
                }
            )
            _next_voice_turn(sid, convo)
            st.rerun()
        except services.ServiceError as exc:
            st.error(formatter.md_escape(str(exc)), icon=":material/error:")


page_header(
    "Mock Interviews",
    "Choose between Proctored Written Interview (Typing) and Proctored Hands-Free Voice Interview, "
    "with camera, microphone, and fullscreen protection.",
)
sid, _ = require_profile()

if (session := st.session_state.get("interview")) is not None:
    _run_typing_interview(sid, session)
elif (convo := st.session_state.get("voice_convo")) is not None:
    _run_voice_interview(sid, convo)
else:
    _setup(sid)
