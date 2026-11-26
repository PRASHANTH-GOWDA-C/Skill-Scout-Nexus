"""Live Webcam & Strict Tab-Switch Proctoring Guard for Skill Scout Nexus.

Responsibilities:
1. Streamlit HTML/JS component (st.components.v1.html):
   - Injects hidden proctoring script into the browser for Quiz and Voice Interview pages.
   - Tab-Switch & Window Blur: Listens to visibilitychange and window.onblur.
     Triggers a loud JS AudioContext beep (880Hz) and increments strike counter in session_state.
   - Copy/Paste Lock: Disables contextmenu (right-click), copy, cut, paste, and dragstart
     on both current iframe and parent window document.
   - Webcam Presence Check: Requests navigator.mediaDevices.getUserMedia({video: true}).
     Draws frames to a hidden canvas every 3s. Checks average brightness and motion.
     If camera is completely black or static for >5 seconds, triggers a CAMERA_ABSENT warning.
2. 3-Strike Rule (ProctorState: clean -> warned -> breached, one object per session):
   - The third strike adds the PROCTOR_BREACH flag.
   - Persists PROCTOR_BREACH to database via database/crud.py (QuizAttempt or voice session).
   - Force-submits the test.
3. Clean warning banners displayed in Streamlit UI.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import streamlit as st
import streamlit.components.v1 as components
from streamlit.runtime.scriptrunner import get_script_run_ctx

from database import crud
from database.connection import DatabaseManager

log = logging.getLogger("core.proctor_guard")

MAX_PROCTOR_STRIKES = 3
FLAG_TAB_SWITCH = "TAB_SWITCH"
FLAG_WINDOW_BLUR = "WINDOW_BLUR"
FLAG_CAMERA_ABSENT = "CAMERA_ABSENT"
FLAG_PROCTOR_BREACH = "PROCTOR_BREACH"

STATE_KEY = "proctor_state"  # the session's ProctorState (piq.ui.PER_STUDENT_KEYS)

Phase = Literal["clean", "warned", "breached"]


@dataclass(frozen=True)
class ProctorState:
    """One assessment's proctoring, as an explicit state machine.

    clean --strike / camera notice--> warned --strike #MAX_PROCTOR_STRIKES--> breached
    Any phase --reset_proctor_state()--> clean. "Breached" is derived from the strike count,
    so a breach without its strikes (or the reverse) cannot be represented.
    """

    strikes: int = 0
    flags: tuple[str, ...] = ()  # first-seen order, no repeats
    warning: str | None = None
    seen_events: frozenset[str] = frozenset()  # query-param events already counted

    @property
    def phase(self) -> Phase:
        if self.strikes >= MAX_PROCTOR_STRIKES:
            return "breached"
        return "warned" if self.strikes or self.warning else "clean"

    def _flagged(self, *new: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self.flags, *new]))

    def with_strike(self, reason: str, detail: str = "") -> ProctorState:
        strikes = self.strikes + 1
        if strikes >= MAX_PROCTOR_STRIKES:
            return replace(
                self,
                strikes=strikes,
                flags=self._flagged(reason, FLAG_PROCTOR_BREACH),
                warning=(
                    f"3 strikes reached ({reason}). Assessment force-submitted due to proctor "
                    "breach."
                ),
            )
        desc = f" ({detail})" if detail else ""
        return replace(
            self,
            strikes=strikes,
            flags=self._flagged(reason),
            warning=(
                f"Proctoring warning: {reason}{desc}. Strike {strikes}/{MAX_PROCTOR_STRIKES}."
            ),
        )

    def with_camera_warning(self, detail: str) -> ProctorState:
        """A camera notice: flagged and shown, but not a strike."""
        return replace(
            self,
            flags=self._flagged(FLAG_CAMERA_ABSENT),
            warning=f"CAMERA_ABSENT: {detail}. Please ensure your face is visible.",
        )

    def with_event_seen(self, event: str) -> ProctorState:
        return replace(self, seen_events=self.seen_events | {event})


# Only for bare mode (no Streamlit session: plain unit tests and scripts). A real session
# always keeps its own state in st.session_state, so one student's strikes can never reach
# another session through this module.
_bare_state = ProctorState()


def _in_session() -> bool:
    return get_script_run_ctx(suppress_warning=True) is not None


def _load() -> ProctorState:
    if _in_session():
        state = st.session_state.get(STATE_KEY)
        return state if isinstance(state, ProctorState) else ProctorState()
    return _bare_state


def _save(state: ProctorState) -> ProctorState:
    global _bare_state
    if _in_session():
        st.session_state[STATE_KEY] = state
    else:
        _bare_state = state
    return state


@dataclass(frozen=True)
class ProctorGuardStatus:
    strikes: int
    is_breached: bool
    warning: str | None
    flags: list[str] = field(default_factory=list)


def proctor_state() -> ProctorState:
    """This session's proctoring state (clean until the first strike or notice)."""
    return _load()


def init_proctor_state() -> None:
    """Start this session's proctoring state if it has none (a no-op otherwise)."""
    _save(_load())


def reset_proctor_state() -> None:
    """Reset proctor strikes, flags, and warnings for a new test."""
    _save(ProctorState())


def get_strikes() -> int:
    return _load().strikes


def is_breached() -> bool:
    return _load().phase == "breached"


def get_warning() -> str | None:
    return _load().warning


def get_proctor_flags() -> list[str]:
    return list(_load().flags)


get_flags = get_proctor_flags  # Alias


def record_strike(reason: str = FLAG_TAB_SWITCH, detail: str = "") -> int:
    """Count one strike; the MAX_PROCTOR_STRIKES-th one breaches. Returns the strike count."""
    return _save(_load().with_strike(reason, detail)).strikes


def record_camera_warning(detail: str = "Webcam absent or black/static for >5s") -> None:
    """Record a camera absence warning without triggering a strike."""
    _save(_load().with_camera_warning(detail))


def save_proctor_breach_to_db(
    student_id: int,
    assessment_type: Literal["quiz", "critic_quiz", "voice"],
    *,
    stream: str = "Engineering",
    domain: str = "General",
    score: int = 0,
    total: int = 5,
    role: str = "Candidate",
    db: DatabaseManager | None = None,
) -> Any:
    """Push PROCTOR_BREACH flag directly to the database (QuizAttempt or VoiceInterviewSession)."""
    flags = list(dict.fromkeys([*get_proctor_flags(), FLAG_PROCTOR_BREACH]))
    if assessment_type in ("quiz", "critic_quiz"):
        return crud.save_quiz_attempt(
            student_id=student_id,
            stream=stream,
            domain=domain,
            score=score,
            total_questions=total,
            critique_notes=("[TEST TERMINATED: Force-submitted due to 3 strikes / PROCTOR_BREACH]"),
            proctor_flags=flags,
            db=db,
        )
    else:
        return crud.save_voice_session(
            student_id=student_id,
            role=role,
            audio_transcript="[TEST TERMINATED: Force-submitted due to PROCTOR_BREACH]",
            rubric_score=0,
            verbal_clarity_score=0.0,
            relevance_score=0.0,
            feedback=(
                "Assessment terminated prematurely due to repeated proctoring violations "
                "(PROCTOR_BREACH)."
            ),
            proctor_flags=flags,
            db=db,
        )


def proctor_html_component(page_type: str = "assessment") -> str:
    """Generate strict self-contained HTML/JS proctoring guard:
    - Enforces live Camera + Microphone permission
    - Enforces Fullscreen mode with blocking overlay until active
    - Blocks tab switching, app/window switching, fullscreen exit, copy/paste, and devtools keys
    - Silent visual alerts (zero annoying background beep/oscillator sound)
    """
    template = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { margin: 0; padding: 0; overflow: hidden; background: transparent; }
  #proctor-canvas { display: none; }
</style>
</head>
<body>
<canvas id="proctor-canvas" width="64" height="48"></canvas>
<script>
(function() {
  const pageType = __PAGE_TYPE__;
  const parentWin = window.parent || window;
  const parentDoc = parentWin.document || document;

  // Avoid duplicate listeners on Streamlit reruns in same session
  if (!parentWin.__piqProctor) {
    parentWin.__piqProctor = { active: false, wasFullscreen: false, strikes: 0, lastAt: 0 };
  }
  const state = parentWin.__piqProctor;
  state.active = true;

  // Ensure live monitor pill & strict fullscreen overlay exist inside parent document
  let pip = parentDoc.getElementById("piq-proctor-pip");
  if (!pip) {
    pip = parentDoc.createElement("div");
    pip.id = "piq-proctor-pip";
    pip.style.cssText = [
      "position:fixed", "bottom:14px", "right:14px", "z-index:99990",
      "background:#111a1c", "color:#e3eae8", "border:1px solid #0d6670",
      "border-radius:10px", "padding:8px 10px", "display:flex",
      "align-items:center", "gap:10px", "font:12px/1.3 system-ui,sans-serif",
      "box-shadow:0 6px 20px rgba(0,0,0,0.35)"
    ].join(";");
    pip.innerHTML =
      '<video id="piq-proctor-vid" autoplay playsinline muted style="width:76px;height:54px;border-radius:6px;background:#000;object-fit:cover;"></video>' +
      '<div>' +
        '<div style="font-weight:700;color:#6ccbc4;">Strict Proctoring ON</div>' +
        '<div id="piq-proctor-hw">Checking Cam &amp; Mic…</div>' +
        '<div id="piq-proctor-fs">Fullscreen required</div>' +
      '</div>';
    parentDoc.body.appendChild(pip);
  }

  let overlay = parentDoc.getElementById("piq-proctor-lock-overlay");
  if (!overlay) {
    overlay = parentDoc.createElement("div");
    overlay.id = "piq-proctor-lock-overlay";
    overlay.style.cssText = [
      "position:fixed", "inset:0", "z-index:99999",
      "background:rgba(12,18,20,0.92)", "color:#f0f5f4",
      "display:flex", "flex-direction:column", "align-items:center", "justify-content:center",
      "text-align:center", "padding:24px", "font-family:system-ui,sans-serif"
    ].join(";");
    overlay.innerHTML =
      '<div style="max-width:480px;background:#1a2528;border:1px solid #1f7f86;border-radius:14px;padding:28px;">' +
        '<h2 style="margin:0 0 10px;color:#6ccbc4;">Proctored Assessment Lock</h2>' +
        '<p id="piq-lock-msg" style="margin:0 0 18px;font-size:14px;line-height:1.5;color:#cbd5d3;">' +
          'Camera, microphone, and fullscreen mode are mandatory. Switching tabs, switching apps, ' +
          'or exiting fullscreen counts toward a 3-strike auto-submission.' +
        '</p>' +
        '<button id="piq-enter-fs-btn" style="background:#0d6670;color:#fff;border:0;border-radius:8px;padding:12px 22px;font-size:15px;font-weight:600;cursor:pointer;">' +
          'Enable Camera, Mic &amp; Enter Fullscreen' +
        '</button>' +
        '<div id="piq-lock-err" style="margin-top:12px;color:#ff8675;font-size:13px;"></div>' +
      '</div>';
    parentDoc.body.appendChild(overlay);
  }

  const vidEl = parentDoc.getElementById("piq-proctor-vid");
  const hwEl = parentDoc.getElementById("piq-proctor-hw");
  const fsEl = parentDoc.getElementById("piq-proctor-fs");
  const errEl = parentDoc.getElementById("piq-lock-err");
  const enterBtn = parentDoc.getElementById("piq-enter-fs-btn");

  function syncOverlay() {
    const fsOk = !!parentDoc.fullscreenElement;
    const camOk = !!(state.stream && state.stream.getVideoTracks().some(t => t.readyState === "live"));
    const micOk = !!(state.stream && state.stream.getAudioTracks().some(t => t.readyState === "live"));
    if (fsOk) state.wasFullscreen = true;
    if (hwEl) hwEl.textContent = (camOk ? "Cam ON" : "Cam OFF") + " · " + (micOk ? "Mic ON" : "Mic OFF");
    if (fsEl) fsEl.textContent = fsOk ? "Fullscreen ON" : "Fullscreen OFF";
    if (overlay) {
      overlay.style.display = (state.active && (!fsOk || !camOk || !micOk)) ? "flex" : "none";
    }
  }

  async function requestPermissionsAndFullscreen() {
    try {
      if (errEl) errEl.textContent = "";
      if (!state.stream || !state.stream.active) {
        state.stream = await navigator.mediaDevices.getUserMedia({
          video: { width: 240, height: 180 },
          audio: true
        });
      }
      if (vidEl && vidEl.srcObject !== state.stream) {
        vidEl.srcObject = state.stream;
        vidEl.play().catch(() => {});
      }
      if (!parentDoc.fullscreenElement && parentDoc.documentElement.requestFullscreen) {
        await parentDoc.documentElement.requestFullscreen();
      }
      syncOverlay();
    } catch (e) {
      if (errEl) {
        errEl.textContent = "Camera, Microphone, and Fullscreen permission are required (" + (e.message || e.name) + ").";
      }
      reportViolation("CAMERA_ABSENT", "Camera or microphone permission denied");
    }
  }

  if (enterBtn) {
    enterBtn.onclick = requestPermissionsAndFullscreen;
  }

  // Try starting media stream automatically if already permitted
  if (!state.stream && navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    navigator.mediaDevices.getUserMedia({ video: { width: 240, height: 180 }, audio: true })
      .then(s => {
        state.stream = s;
        if (vidEl) { vidEl.srcObject = s; vidEl.play().catch(() => {}); }
        syncOverlay();
      })
      .catch(() => syncOverlay());
  } else if (state.stream && vidEl) {
    vidEl.srcObject = state.stream;
    syncOverlay();
  }

  function reportViolation(type, detail) {
    if (!state.active) return;
    const now = Date.now();
    if (now - state.lastAt < 1800) return;
    state.lastAt = now;
    if (type === "TAB_SWITCH" || type === "WINDOW_BLUR" || type === "FULLSCREEN_EXIT" || type === "CAMERA_ABSENT") {
      state.strikes += 1;
    }
    try {
      const u = new URL(parentWin.location.href);
      u.searchParams.set("proctor_event", type + ":" + now);
      if (state.strikes >= 3) {
        // Breach reached: force reload URL so Streamlit submits immediately
        parentWin.location.href = u.href;
      } else {
        parentWin.history.replaceState({}, "", u.href);
      }
    } catch (e) {}
  }

  if (!parentWin.__piqProctorBound) {
    parentWin.__piqProctorBound = true;
    const blockEvt = (e) => {
      if (!state.active) return;
      e.preventDefault();
      e.stopPropagation();
      return false;
    };
    ["contextmenu", "copy", "cut", "paste", "dragstart"].forEach(evt => {
      parentDoc.addEventListener(evt, blockEvt, true);
    });

    parentDoc.addEventListener("keydown", function(e) {
      if (!state.active) return;
      if (
        e.key === "F12" || e.key === "F11" || e.key === "PrintScreen" ||
        ((e.ctrlKey || e.metaKey) && ["c","v","x","t","w","tab","u","i","j"].includes((e.key || "").toLowerCase()))
      ) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);

    parentDoc.addEventListener("visibilitychange", function() {
      if (state.active && parentDoc.visibilityState === "hidden") {
        reportViolation("TAB_SWITCH", "Tab switched or browser minimized");
      }
    }, true);

    parentWin.addEventListener("blur", function() {
      if (!state.active) return;
      setTimeout(() => {
        const ae = parentDoc.activeElement;
        if (ae && ae.tagName === "IFRAME") return;
        reportViolation("WINDOW_BLUR", "Application or window lost focus");
      }, 90);
    }, true);

    parentDoc.addEventListener("fullscreenchange", function() {
      syncOverlay();
      if (state.active && state.wasFullscreen && !parentDoc.fullscreenElement) {
        reportViolation("FULLSCREEN_EXIT", "Exited fullscreen mode during assessment");
      }
    }, true);
  }

  // Camera lens cover check (pitch black check only; stillness never penalized)
  const canvas = document.getElementById("proctor-canvas");
  const ctx = canvas ? canvas.getContext("2d") : null;
  if (ctx) {
    setInterval(function() {
      syncOverlay();
      if (!state.active || !vidEl || vidEl.readyState < 2) return;
      try {
        ctx.drawImage(vidEl, 0, 0, canvas.width, canvas.height);
        const d = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        let sum = 0;
        for (let i = 0; i < d.length; i += 4) sum += (d[i] + d[i+1] + d[i+2]) / 3;
        const avg = sum / (d.length / 4);
        if (avg < 4) {
          state.darkCount = (state.darkCount || 0) + 1;
          if (state.darkCount >= 2) {
            reportViolation("CAMERA_ABSENT", "Camera covered or completely black");
            state.darkCount = 0;
          }
        } else {
          state.darkCount = 0;
        }
      } catch (e) {}
    }, 3000);
  }
  syncOverlay();
})();
</script>
</body>
</html>"""
    return template.replace("__PAGE_TYPE__", json.dumps(page_type))


def clear_proctor_overlay() -> None:
    """Remove any parent-document proctor overlay/monitor and release fullscreen after exit/completion."""
    components.html(
        """<script>
        (function(){
          try {
            const p = window.parent || window;
            const d = p.document || document;
            if (p.__piqProctor) { p.__piqProctor.active = false; p.__piqProctor.wasFullscreen = false; }
            const ov = d.getElementById("piq-proctor-lock-overlay");
            if (ov) ov.remove();
            const pip = d.getElementById("piq-proctor-pip");
            if (pip) pip.remove();
            if (d.fullscreenElement && d.exitFullscreen) { d.exitFullscreen().catch(()=>{}); }
          } catch(e){}
        })();
        </script>""",
        height=0,
        width=0,
    )


def render_proctor_guard(
    page_type: str = "assessment",
    on_breach: Callable[[], None] | None = None,
) -> ProctorGuardStatus:
    """Inject hidden proctoring script, read incoming strike events, and handle breach callbacks."""
    raw_event = st.query_params.get("proctor_event")
    if raw_event and raw_event not in _load().seen_events:
        _save(_load().with_event_seen(raw_event))
        event_type = raw_event.split(":")[0] if ":" in raw_event else raw_event
        if event_type in (FLAG_TAB_SWITCH, FLAG_WINDOW_BLUR, "FULLSCREEN_EXIT", FLAG_CAMERA_ABSENT):
            record_strike(event_type, "Proctoring rule violation detected")
        with contextlib.suppress(Exception):
            del st.query_params["proctor_event"]

    if is_breached() and on_breach:
        on_breach()

    components.html(proctor_html_component(page_type), height=0, width=0)

    return ProctorGuardStatus(
        strikes=get_strikes(),
        is_breached=is_breached(),
        warning=get_warning(),
        flags=get_proctor_flags(),
    )


def render_warning_banner() -> None:
    """Display a clean Streamlit warning banner for strikes, camera warnings, or proctor breach."""
    if is_breached():
        st.error(
            "🚨 **PROCTOR BREACH:** Maximum strikes reached (3/3). This assessment has been "
            "flagged and force-submitted.",
            icon=":material/gavel:",
        )
    elif get_strikes() > 0:
        strikes = get_strikes()
        st.warning(
            f"⚠️ **PROCTOR WARNING:** Strike {strikes}/{MAX_PROCTOR_STRIKES} recorded. "
            "Tab-switching, minimizing the window, or losing focus is strictly prohibited. "
            "3 strikes will automatically submit your assessment.",
            icon=":material/warning:",
        )
    elif warning := get_warning():
        st.warning(f"⚠️ **PROCTOR NOTICE:** {warning}", icon=":material/videocam_off:")
