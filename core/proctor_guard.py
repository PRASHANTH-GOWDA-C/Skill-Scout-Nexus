"""Live Webcam, AI Vision, Audio & Strict Switch Proctoring Guard for Skill Scout Nexus.

Responsibilities:
1. Cross-OS Pop-up & Enforcement (st.components.v1.html + hidden Streamlit widget bridge):
   - Shows immediate setup pop-up modal requiring Camera, Microphone, and Fullscreen mode.
   - Attempts automatic fullscreen & media acquisition on mount; supports standard, webkit,
     moz, and ms fullscreen APIs + Keyboard Lock (where supported) across Windows, macOS, Linux.
   - Reliable multi-test controller architecture (`parentWin.__piqProctorCtrl`) so flags work
     identically on the 1st, 2nd, 3rd, and Nth quiz/interview without stale iframe closures.
   - Immediate violation reporting via hidden Streamlit widget bridge (zero page reload,
     preserves `st.session_state`).
2. Multi-Signal Proctoring Detection:
   - Tab Switch & Page Hide (`TAB_SWITCH`)
   - App / Virtual Desktop / Gesture Switch (`APP_SWITCH`: window blur, Alt+Tab, Cmd+Tab,
     Ctrl+Tab, Ctrl+Alt+Arrows, 3/4-finger trackpad gestures, Mission Control, Task View)
   - Fullscreen Exit (`FULLSCREEN_EXIT`)
   - Camera / Mic Revoked or Covered (`CAMERA_ABSENT`, `MIC_ABSENT`)
   - AI Vision (`@mediapipe/tasks-vision` BlazeFace + zero-dependency Canvas CV fallback):
     - Multiple people in frame (`MULTIPLE_PERSONS`)
     - Person absent / no face (`NO_FACE`)
     - Head turned / looking away (`LOOKING_AWAY`)
     - Sudden large human/background movement (`SUSPICIOUS_MOVEMENT`)
   - Web Audio External Noise Monitor (`LOUD_NOISE`, pauses during AI speech in voice mode)
3. 3-Strike Rule (ProctorState: clean -> warned -> breached, one object per session):
   - 3 flags immediately end and force-submit the quiz or mock interview (both typing & voice).
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
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
FLAG_APP_SWITCH = "APP_SWITCH"
FLAG_FULLSCREEN_EXIT = "FULLSCREEN_EXIT"
FLAG_CAMERA_ABSENT = "CAMERA_ABSENT"
FLAG_MIC_ABSENT = "MIC_ABSENT"
FLAG_MULTIPLE_PERSONS = "MULTIPLE_PERSONS"
FLAG_NO_FACE = "NO_FACE"
FLAG_LOOKING_AWAY = "LOOKING_AWAY"
FLAG_SUSPICIOUS_MOVEMENT = "SUSPICIOUS_MOVEMENT"
FLAG_LOUD_NOISE = "LOUD_NOISE"
FLAG_PROCTOR_BREACH = "PROCTOR_BREACH"

STRIKE_EVENT_TYPES = frozenset(
    {
        FLAG_TAB_SWITCH,
        FLAG_WINDOW_BLUR,
        FLAG_APP_SWITCH,
        FLAG_FULLSCREEN_EXIT,
        FLAG_CAMERA_ABSENT,
        FLAG_MIC_ABSENT,
        FLAG_MULTIPLE_PERSONS,
        FLAG_NO_FACE,
        FLAG_LOOKING_AWAY,
        FLAG_SUSPICIOUS_MOVEMENT,
        FLAG_LOUD_NOISE,
    }
)

STATE_KEY = "proctor_state"  # the session's ProctorState (piq.ui.PER_STUDENT_KEYS)
BRIDGE_KEY = "_piq_proctor_bridge"

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
    seen_events: frozenset[str] = frozenset()  # bridge/query-param events already counted
    session_nonce: str = field(default_factory=lambda: uuid.uuid4().hex[:10], compare=False)

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
    if _in_session():
        st.session_state.pop(BRIDGE_KEY, None)


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


def proctor_html_component(page_type: str = "assessment", session_nonce: str = "") -> str:
    """Generate strict self-contained HTML/JS proctoring guard:
    - Pop-up notification & blocking setup overlay for Camera, Microphone, and auto Fullscreen
    - Cross-OS fullscreen API (standard, webkit, moz, ms) + Keyboard Lock when supported
    - Live controller shim on `window.parent.__piqProctorCtrl` so tests 1, 2, 3... always re-arm
    - Immediate violation flagging via Streamlit widget bridge (no page reload / session loss)
    - AI Vision (MediaPipe BlazeFace + offline Canvas CV fallback) for multiple persons, no face,
      looking away, movement, and covered lens + Web Audio external loud noise monitor
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
<canvas id="proctor-canvas" width="160" height="120"></canvas>
<script>
(function() {
  const pageType = __PAGE_TYPE__;
  const maxStrikes = __MAX_STRIKES__;
  const bridgeLabel = __BRIDGE_LABEL__;
  const sessionNonce = __SESSION_NONCE__;
  const isVoiceMode = pageType === "voice_interview";
  const parentWin = window.parent || window;
  const parentDoc = parentWin.document || document;

  // Inject CSS once into parent <head> to cleanly hide the bridge input & zero-height proctor iframe
  if (!parentDoc.getElementById("piq-proctor-style")) {
    const st = parentDoc.createElement("style");
    st.id = "piq-proctor-style";
    st.textContent =
      '[data-testid="stTextInput"]:has(input[aria-label="' + bridgeLabel + '"]),' +
      '[data-testid="stElementContainer"]:has(input[aria-label="' + bridgeLabel + '"]),' +
      '[data-testid="stElementContainer"]:has(iframe[height="0"]) {' +
        'position:fixed !important;left:-9999px !important;top:-9999px !important;' +
        'width:1px !important;height:1px !important;opacity:0 !important;' +
        'pointer-events:none !important;overflow:hidden !important;margin:0 !important;padding:0 !important;' +
      '}';
    parentDoc.head.appendChild(st);
  }

  function hideBridgeInput() {
    try {
      const inp = parentDoc.querySelector('input[aria-label="' + bridgeLabel + '"]');
      if (inp) {
        const box = inp.closest('[data-testid="stElementContainer"]') || inp.closest('[data-testid="stTextInput"]') || inp.parentElement;
        if (box && box.dataset.piqHidden !== "1") {
          box.dataset.piqHidden = "1";
          box.style.cssText = "position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;opacity:0;pointer-events:none;overflow:hidden;margin:0;padding:0;";
        }
      }
    } catch (e) {}
  }
  hideBridgeInput();

  function getFullscreenEl() {
    return (
      parentDoc.fullscreenElement ||
      parentDoc.webkitFullscreenElement ||
      parentDoc.mozFullScreenElement ||
      parentDoc.msFullscreenElement ||
      null
    );
  }

  function requestParentFullscreen() {
    const el = parentDoc.documentElement;
    const req =
      el.requestFullscreen ||
      el.webkitRequestFullscreen ||
      el.mozRequestFullScreen ||
      el.msRequestFullscreen;
    if (!req) return Promise.resolve();
    try {
      const res = req.call(el);
      if (parentWin.navigator && parentWin.navigator.keyboard && parentWin.navigator.keyboard.lock) {
        parentWin.navigator.keyboard.lock(["Escape"]).catch(() => {});
      }
      return res && typeof res.then === "function" ? res : Promise.resolve();
    } catch (e) {
      return Promise.reject(e);
    }
  }

  // Persistent cross-rerun state on parentWin, reset whenever a new assessment nonce starts
  if (!parentWin.__piqProctor) {
    parentWin.__piqProctor = {
      nonce: "",
      active: false,
      armed: false,
      wasFullscreen: false,
      wasFocused: true,
      strikes: 0,
      lastAt: 0,
      seq: 0,
      darkCount: 0,
      noFaceCount: 0,
      multiFaceCount: 0,
      lookAwayCount: 0,
      moveCount: 0,
      noiseCount: 0,
      lastFaceCenter: null,
      lastVisionTs: 0,
      prevLuma: null,
      stream: null,
      audioCtx: null,
      analyser: null,
      audioData: null,
      watchTimer: null,
      visionTimer: null,
      audioTimer: null,
      uiCache: {}
    };
  }
  const state = parentWin.__piqProctor;
  if (sessionNonce && state.nonce !== sessionNonce) {
    state.nonce = sessionNonce;
    state.armed = false;
    state.wasFullscreen = false;
    state.wasFocused = true;
    state.strikes = 0;
    state.lastAt = 0;
    state.darkCount = 0;
    state.noFaceCount = 0;
    state.multiFaceCount = 0;
    state.lookAwayCount = 0;
    state.moveCount = 0;
    state.noiseCount = 0;
    state.lastFaceCenter = null;
    state.prevLuma = null;
    state.uiCache = {};
  }
  state.active = true;

  // Ensure live monitor pill, toast, and setup pop-up exist in parentDoc
  let pip = parentDoc.getElementById("piq-proctor-pip");
  if (!pip) {
    pip = parentDoc.createElement("div");
    pip.id = "piq-proctor-pip";
    pip.style.cssText = [
      "position:fixed", "bottom:14px", "right:14px", "z-index:99990",
      "background:#111a1c", "color:#e3eae8", "border:1px solid #0d6670",
      "border-radius:10px", "padding:8px 11px", "display:flex",
      "align-items:center", "gap:10px", "font:12px/1.32 system-ui,-apple-system,sans-serif",
      "box-shadow:0 6px 20px rgba(0,0,0,0.35)", "pointer-events:none"
    ].join(";");
    pip.innerHTML =
      '<video id="piq-proctor-vid" autoplay playsinline muted style="width:88px;height:64px;border-radius:6px;background:#000;object-fit:cover;transform:scaleX(-1);"></video>' +
      '<div>' +
        '<div style="font-weight:700;color:#6ccbc4;">Strict Proctoring ON</div>' +
        '<div id="piq-proctor-hw">Checking Cam &amp; Mic…</div>' +
        '<div id="piq-proctor-fs">Fullscreen required</div>' +
        '<div id="piq-proctor-vision" style="color:#a0aeab;">Vision: Initializing…</div>' +
        '<div id="piq-proctor-strikes" style="color:#f6c453;font-weight:700;">Flags: 0/' + maxStrikes + '</div>' +
      '</div>';
    parentDoc.body.appendChild(pip);
  }

  let toast = parentDoc.getElementById("piq-proctor-toast");
  if (!toast) {
    toast = parentDoc.createElement("div");
    toast.id = "piq-proctor-toast";
    toast.style.cssText = [
      "position:fixed", "top:18px", "right:18px", "z-index:100001",
      "max-width:440px", "background:#7f1d1d", "color:#fef2f2",
      "border:1px solid #f87171", "border-radius:10px", "padding:12px 16px",
      "font:600 13px/1.4 system-ui,-apple-system,sans-serif",
      "box-shadow:0 10px 28px rgba(0,0,0,0.45)", "display:none", "pointer-events:none"
    ].join(";");
    parentDoc.body.appendChild(toast);
  }

  function showToast(msg) {
    try {
      const t = parentDoc.getElementById("piq-proctor-toast");
      if (!t) return;
      t.textContent = msg;
      t.style.display = "block";
      clearTimeout(state.toastTimer);
      state.toastTimer = setTimeout(() => {
        const cur = parentDoc.getElementById("piq-proctor-toast");
        if (cur) cur.style.display = "none";
      }, 4200);
    } catch (e) {}
  }

  let overlay = parentDoc.getElementById("piq-proctor-lock-overlay");
  if (!overlay) {
    overlay = parentDoc.createElement("div");
    overlay.id = "piq-proctor-lock-overlay";
    overlay.style.cssText = [
      "position:fixed", "inset:0", "z-index:99999",
      "background:rgba(12,18,20,0.92)", "color:#f0f5f4",
      "display:flex", "flex-direction:column", "align-items:center", "justify-content:center",
      "text-align:center", "padding:24px", "font-family:system-ui,-apple-system,sans-serif"
    ].join(";");
    overlay.innerHTML =
      '<div style="max-width:520px;width:100%;background:#1a2528;border:1px solid #1f7f86;border-radius:14px;padding:28px;box-shadow:0 16px 40px rgba(0,0,0,0.5);">' +
        '<div style="font-size:28px;margin-bottom:6px;">🛡️</div>' +
        '<h2 style="margin:0 0 8px;color:#6ccbc4;font-size:20px;">Turn On Camera, Mic &amp; Fullscreen</h2>' +
        '<p id="piq-lock-msg" style="margin:0 0 16px;font-size:14px;line-height:1.5;color:#cbd5d3;">' +
          'This proctored assessment monitors <strong>Camera Vision</strong> (single person, gaze, movement), ' +
          '<strong>Microphone Audio</strong> (external noise), and <strong>Fullscreen mode</strong>. ' +
          'Tab/app/desktop switching (Alt+Tab, Cmd+Tab, 3/4-finger swipe) or vision/audio violations add a flag. <strong>' + maxStrikes + ' flags end the test.</strong>' +
        '</p>' +
        '<div id="piq-lock-checklist" style="text-align:left;background:#111a1c;border:1px solid #26383d;border-radius:10px;padding:12px 14px;margin:0 0 18px;font-size:13px;line-height:1.8;">' +
          '<div id="piq-chk-cam">📷 Camera: Checking…</div>' +
          '<div id="piq-chk-mic">🎙️ Microphone: Checking…</div>' +
          '<div id="piq-chk-fs">⛶ Fullscreen: OFF (Click below to activate)</div>' +
        '</div>' +
        '<button id="piq-enter-fs-btn" style="width:100%;background:#0d6670;color:#fff;border:0;border-radius:8px;padding:13px 22px;font-size:15px;font-weight:600;cursor:pointer;">' +
          'Allow Camera, Mic &amp; Enter Fullscreen' +
        '</button>' +
        '<div id="piq-lock-err" style="margin-top:12px;color:#ff8675;font-size:13px;"></div>' +
      '</div>';
    parentDoc.body.appendChild(overlay);
  }

  function setTextCached(id, text) {
    if (state.uiCache[id] === text) return;
    state.uiCache[id] = text;
    const el = parentDoc.getElementById(id);
    if (el) el.textContent = text;
  }

  function setHtmlCached(id, html) {
    if (state.uiCache["h:" + id] === html) return;
    state.uiCache["h:" + id] = html;
    const el = parentDoc.getElementById(id);
    if (el) el.innerHTML = html;
  }

  function syncOverlay() {
    hideBridgeInput();
    const fsOk = !!getFullscreenEl();
    const camOk = !!(state.stream && state.stream.getVideoTracks().some(t => t.readyState === "live" && t.enabled));
    const micOk = !!(state.stream && state.stream.getAudioTracks().some(t => t.readyState === "live" && t.enabled));
    if (fsOk) state.wasFullscreen = true;
    if (fsOk && camOk && micOk && !state.armed) {
      state.armed = true;
      state.wasFocused = parentDoc.hasFocus();
    }
    setTextCached("piq-proctor-hw", (camOk ? "Cam ON" : "Cam OFF") + " · " + (micOk ? "Mic ON" : "Mic OFF"));
    setTextCached("piq-proctor-fs", fsOk ? "Fullscreen ON" : "Fullscreen OFF");
    setTextCached("piq-proctor-strikes", "Flags: " + state.strikes + "/" + maxStrikes);
    setHtmlCached("piq-chk-cam", camOk ? "📷 Camera: <strong style='color:#4ade80;'>ON</strong>" : "📷 Camera: <strong style='color:#fbbf24;'>OFF — Permission needed</strong>");
    setHtmlCached("piq-chk-mic", micOk ? "🎙️ Microphone: <strong style='color:#4ade80;'>ON</strong>" : "🎙️ Microphone: <strong style='color:#fbbf24;'>OFF — Permission needed</strong>");
    setHtmlCached("piq-chk-fs", fsOk ? "⛶ Fullscreen: <strong style='color:#4ade80;'>ON</strong>" : "⛶ Fullscreen: <strong style='color:#fbbf24;'>OFF — Click button to enter</strong>");

    const ov = parentDoc.getElementById("piq-proctor-lock-overlay");
    const wantDisplay = (state.active && (!fsOk || !camOk || !micOk)) ? "flex" : "none";
    if (ov && state.uiCache["ov"] !== wantDisplay) {
      state.uiCache["ov"] = wantDisplay;
      ov.style.display = wantDisplay;
    }
  }

  function setupAudioAnalyser(stream) {
    try {
      if (!stream || !stream.getAudioTracks().length) return;
      const AC = parentWin.AudioContext || parentWin.webkitAudioContext;
      if (!AC) return;
      if (!state.audioCtx || state.audioCtx.state === "closed") {
        state.audioCtx = new AC();
        state.analyserStreamId = null;
      }
      if (state.audioCtx.state === "suspended") {
        state.audioCtx.resume().catch(() => {});
      }
      if (state.analyserStreamId !== stream.id) {
        const src = state.audioCtx.createMediaStreamSource(stream);
        const analyser = state.audioCtx.createAnalyser();
        analyser.fftSize = 1024;
        src.connect(analyser);
        state.analyser = analyser;
        state.audioData = new Uint8Array(analyser.fftSize);
        state.analyserStreamId = stream.id;
      }
    } catch (e) {}
  }

  function bindTrackEndHandlers(stream) {
    if (!stream) return;
    stream.getVideoTracks().forEach(t => {
      t.onended = () => {
        syncOverlay();
        if (state.active && state.armed) reportViolation("CAMERA_ABSENT", "Camera was turned off or disconnected");
      };
    });
    stream.getAudioTracks().forEach(t => {
      t.onended = () => {
        syncOverlay();
        if (state.active && state.armed) reportViolation("MIC_ABSENT", "Microphone was turned off or disconnected");
      };
    });
  }

  async function acquireMedia() {
    if (state.stream && state.stream.active &&
        state.stream.getVideoTracks().some(t => t.readyState === "live") &&
        state.stream.getAudioTracks().some(t => t.readyState === "live")) {
      const v = parentDoc.getElementById("piq-proctor-vid");
      if (v && v.srcObject !== state.stream) {
        v.srcObject = state.stream;
        v.play().catch(() => {});
      }
      setupAudioAnalyser(state.stream);
      return state.stream;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("Media devices API unavailable in this browser");
    }
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 320 }, height: { ideal: 240 }, facingMode: "user" },
        audio: { echoCancellation: true, noiseSuppression: false }
      });
    } catch (err) {
      if (err && (err.name === "OverconstrainedError" || err.name === "ConstraintNotSatisfiedError")) {
        state.stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
      } else {
        throw err;
      }
    }
    bindTrackEndHandlers(state.stream);
    setupAudioAnalyser(state.stream);
    const v = parentDoc.getElementById("piq-proctor-vid");
    if (v && v.srcObject !== state.stream) {
      v.srcObject = state.stream;
      v.play().catch(() => {});
    }
    return state.stream;
  }

  async function requestPermissionsAndFullscreen() {
    const errEl = parentDoc.getElementById("piq-lock-err");
    try {
      if (errEl) errEl.textContent = "";
      await acquireMedia();
      if (state.audioCtx && state.audioCtx.state === "suspended") {
        await state.audioCtx.resume().catch(() => {});
      }
      if (!getFullscreenEl()) {
        await requestParentFullscreen();
      }
      syncOverlay();
      initFaceDetector();
    } catch (e) {
      if (errEl) {
        errEl.textContent = "Camera, Microphone, and Fullscreen permission are required (" + (e.message || e.name || "denied") + ").";
      }
      syncOverlay();
    }
  }

  const enterBtn = parentDoc.getElementById("piq-enter-fs-btn");
  if (enterBtn) {
    enterBtn.onclick = requestPermissionsAndFullscreen;
  }

  // Load MediaPipe BlazeFace inside parentWin once (async, non-blocking, offline-safe fallback)
  function initFaceDetector() {
    if (parentWin.__piqFaceDetector || parentWin.__piqFaceDetectorPromise || parentWin.__piqFaceDetectorFailed) return;
    if (typeof navigator !== "undefined" && navigator.onLine === false) {
      parentWin.__piqFaceDetectorFailed = true;
      return;
    }
    try {
      const dynImport = new parentWin.Function("u", "return import(u)");
      parentWin.__piqFaceDetectorPromise = dynImport(
        "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs"
      ).then(async (vision) => {
        const fileset = await vision.FilesetResolver.forVisionTasks(
          "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm"
        );
        const det = await vision.FaceDetector.createFromOptions(fileset, {
          baseOptions: {
            modelAssetPath:
              "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
            delegate: "CPU"
          },
          runningMode: "VIDEO",
          minDetectionConfidence: 0.45,
          minSuppressionThreshold: 0.35
        });
        parentWin.__piqFaceDetector = det;
        return det;
      }).catch(() => {
        parentWin.__piqFaceDetectorFailed = true;
        return null;
      });
    } catch (e) {
      parentWin.__piqFaceDetectorFailed = true;
    }
  }

  // Attempt automatic media + fullscreen on mount (falls back cleanly to setup popup if gesture needed)
  acquireMedia()
    .then(() => {
      initFaceDetector();
      if (!getFullscreenEl()) {
        return requestParentFullscreen().catch(() => {});
      }
    })
    .catch(() => {})
    .finally(() => syncOverlay());

  function sendBridgeEvent(payload) {
    try {
      const inp = parentDoc.querySelector('input[aria-label="' + bridgeLabel + '"]');
      if (inp) {
        const setter = Object.getOwnPropertyDescriptor(parentWin.HTMLInputElement.prototype, "value").set;
        setter.call(inp, payload);
        inp.dispatchEvent(new parentWin.Event("input", { bubbles: true }));
        inp.dispatchEvent(new parentWin.KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
        inp.dispatchEvent(new parentWin.KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
        inp.dispatchEvent(new parentWin.Event("change", { bubbles: true }));
        inp.dispatchEvent(new parentWin.FocusEvent("blur", { bubbles: false }));
        return true;
      }
    } catch (e) {}
    return false;
  }

  function reportViolation(type, detail) {
    if (!state.active || !state.armed) return;
    const now = Date.now();
    if (now - state.lastAt < 2200) return;
    state.lastAt = now;
    state.seq = (state.seq || 0) + 1;
    state.strikes += 1;
    syncOverlay();
    if (state.strikes >= maxStrikes) {
      showToast("🚨 Flag " + state.strikes + "/" + maxStrikes + " (" + type + "): Ending assessment…");
    } else {
      showToast("⚠️ Proctor Flag " + state.strikes + "/" + maxStrikes + ": " + type + " — " + detail);
    }
    const payload = type + ":" + now + "_" + state.seq + ":" + detail;
    sendBridgeEvent(payload);
  }

  // Update the live controller on parentWin on EVERY mount/rerun so listeners NEVER call a dead iframe!
  parentWin.__piqProctorCtrl = {
    blockEvt(e) {
      if (!state.active) return;
      e.preventDefault();
      e.stopPropagation();
      return false;
    },
    onKeyDown(e) {
      if (!state.active) return;
      const k = (e.key || "").toLowerCase();
      const isSwitchCombo =
        (e.altKey && (k === "tab" || k === "escape" || k === "f4")) ||
        ((e.ctrlKey || e.metaKey) && k === "tab") ||
        (e.ctrlKey && e.altKey && k.startsWith("arrow")) ||
        (e.metaKey && e.ctrlKey && k.startsWith("arrow"));
      const isBlockedShortcut =
        isSwitchCombo ||
        e.key === "F12" || e.key === "F11" || e.key === "PrintScreen" ||
        ((e.ctrlKey || e.metaKey) && ["c","v","x","t","w","n","p","u","i","j","k","s","r"].includes(k));
      if (isBlockedShortcut) {
        e.preventDefault();
        e.stopPropagation();
      }
      if (isSwitchCombo && state.armed) {
        reportViolation("APP_SWITCH", "Prohibited window or desktop switch shortcut");
      }
    },
    onVisibility() {
      if (state.active && state.armed && parentDoc.visibilityState === "hidden") {
        reportViolation("TAB_SWITCH", "Switched browser tab or minimized window");
      }
    },
    onPageHide() {
      if (state.active && state.armed) {
        reportViolation("TAB_SWITCH", "Left assessment page");
      }
    },
    onWindowBlur(e) {
      if (!state.active || !state.armed) return;
      if (e && e.target !== parentWin) return;
      setTimeout(() => {
        if (!state.active || !state.armed) return;
        if (!parentDoc.hasFocus() || parentDoc.visibilityState === "hidden") {
          state.wasFocused = false;
          reportViolation("APP_SWITCH", "Switched application, virtual desktop, or lost window focus");
        }
      }, 80);
    },
    onWindowFocus() {
      state.wasFocused = true;
      syncOverlay();
    },
    onFullscreenChange() {
      syncOverlay();
      if (state.active && state.armed && state.wasFullscreen && !getFullscreenEl()) {
        reportViolation("FULLSCREEN_EXIT", "Exited fullscreen mode during assessment");
      }
    }
  };

  if (!parentWin.__piqProctorShimBound) {
    parentWin.__piqProctorShimBound = true;
    ["contextmenu", "copy", "cut", "paste", "dragstart"].forEach(evt => {
      parentDoc.addEventListener(evt, (e) => parentWin.__piqProctorCtrl?.blockEvt?.(e), true);
    });
    parentDoc.addEventListener("keydown", (e) => parentWin.__piqProctorCtrl?.onKeyDown?.(e), true);
    parentDoc.addEventListener("visibilitychange", () => parentWin.__piqProctorCtrl?.onVisibility?.(), true);
    parentWin.addEventListener("pagehide", () => parentWin.__piqProctorCtrl?.onPageHide?.(), true);
    parentWin.addEventListener("blur", (e) => parentWin.__piqProctorCtrl?.onWindowBlur?.(e), true);
    parentWin.addEventListener("focus", () => parentWin.__piqProctorCtrl?.onWindowFocus?.(), true);
    ["fullscreenchange", "webkitfullscreenchange", "mozfullscreenchange", "MSFullscreenChange"].forEach(evt => {
      parentDoc.addEventListener(evt, () => parentWin.__piqProctorCtrl?.onFullscreenChange?.(), true);
    });
  }

  // Clear any old timers from prior test/rerun before starting fresh timers in the live iframe
  clearInterval(state.watchTimer);
  clearInterval(state.visionTimer);
  clearInterval(state.audioTimer);

  // 1. Fast focus + fullscreen watchdog (450ms) catches 3/4-finger OS trackpad gestures & desktop switches
  state.watchTimer = setInterval(function() {
    if (!state.active) return;
    syncOverlay();
    if (!state.armed) return;
    const focused = parentDoc.hasFocus();
    if ((!focused || parentDoc.visibilityState === "hidden") && state.wasFocused) {
      state.wasFocused = false;
      reportViolation("APP_SWITCH", "Window/desktop focus lost (Alt+Tab, gesture, or app switch)");
    } else if (focused && parentDoc.visibilityState === "visible") {
      state.wasFocused = true;
    }
    if (state.wasFullscreen && !getFullscreenEl()) {
      reportViolation("FULLSCREEN_EXIT", "Fullscreen mode exited during assessment");
    }
  }, 450);

  // 2. Hybrid AI Vision Engine (MediaPipe BlazeFace + Zero-Dependency Canvas CV fallback, every 750ms)
  const canvas = document.getElementById("proctor-canvas");
  const ctx = canvas ? canvas.getContext("2d", { willReadFrequently: true }) : null;

  function runCanvasFallbackCV(d, w, h) {
    // Check average brightness, frame-to-frame human movement, and dual skin-tone head clusters
    const totalPx = w * h;
    let sumLuma = 0;
    let diffPx = 0;
    const bins = new Float32Array(24);
    const luma = new Uint8Array(totalPx);
    let skinPx = 0;

    for (let y = 0; y < h; y++) {
      const upper = y < Math.floor(h * 0.85);
      for (let x = 0; x < w; x++) {
        const idx = (y * w + x) * 4;
        const r = d[idx], g = d[idx + 1], b = d[idx + 2];
        const yv = (0.299 * r + 0.587 * g + 0.114 * b);
        const p = y * w + x;
        luma[p] = yv;
        sumLuma += yv;
        if (state.prevLuma && Math.abs(yv - state.prevLuma[p]) > 26) {
          diffPx++;
        }
        if (upper && yv > 45) {
          const cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b;
          const cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b;
          if (cb >= 77 && cb <= 128 && cr >= 133 && cr <= 176) {
            skinPx++;
            bins[Math.min(23, Math.floor((x / w) * 24))]++;
          }
        }
      }
    }
    const prevReady = !!state.prevLuma;
    state.prevLuma = luma;
    const avgLuma = sumLuma / totalPx;
    const motionFrac = prevReady ? diffPx / totalPx : 0;

    // Count distinct horizontal skin clusters (two people sitting side-by-side)
    let clusters = 0;
    let inCluster = false;
    let clusterArea = 0;
    let gapBins = 0;
    const minColHeight = h * 0.14;
    for (let i = 0; i < 24; i++) {
      if (bins[i] >= minColHeight) {
        if (!inCluster) {
          if (clusters === 0 || gapBins >= 2) {
            inCluster = true;
            clusterArea = bins[i];
          }
        } else {
          clusterArea += bins[i];
        }
        gapBins = 0;
      } else {
        if (inCluster && clusterArea >= totalPx * 0.015) {
          clusters++;
        }
        inCluster = false;
        clusterArea = 0;
        gapBins++;
      }
    }
    if (inCluster && clusterArea >= totalPx * 0.015) clusters++;

    return { avgLuma, motionFrac, skinFrac: skinPx / totalPx, clusters };
  }

  state.visionTimer = setInterval(function() {
    const vid = parentDoc.getElementById("piq-proctor-vid");
    if (!state.active || !state.armed || !ctx || !vid || vid.readyState < 2 || !vid.videoWidth) return;
    try {
      ctx.drawImage(vid, 0, 0, canvas.width, canvas.height);
      const d = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      const cv = runCanvasFallbackCV(d, canvas.width, canvas.height);

      // Pitch-black / lens covered check
      if (cv.avgLuma < 6) {
        state.darkCount++;
        setTextCached("piq-proctor-vision", "⚠️ Camera covered/dark!");
        if (state.darkCount >= 2) {
          reportViolation("CAMERA_ABSENT", "Camera covered or completely dark");
          state.darkCount = 0;
        }
        return;
      }
      state.darkCount = 0;

      // Primary: MediaPipe BlazeFace neural detection
      const det = parentWin.__piqFaceDetector;
      if (det) {
        const ts = Math.max(parentWin.performance.now(), (state.lastVisionTs || 0) + 1);
        state.lastVisionTs = ts;
        const res = det.detectForVideo(vid, ts);
        const raw = (res && res.detections) ? res.detections : [];
        // Deduplicate heavily overlapping detections
        const faces = [];
        for (const f of raw) {
          const score = (f.categories && f.categories[0]) ? f.categories[0].score : 0.5;
          if (score < 0.45 || !f.boundingBox) continue;
          const bb = f.boundingBox;
          const cx = bb.originX + bb.width / 2;
          const cy = bb.originY + bb.height / 2;
          const dup = faces.some(existing => {
            const eb = existing.boundingBox;
            const ecx = eb.originX + eb.width / 2;
            const ecy = eb.originY + eb.height / 2;
            return Math.hypot(cx - ecx, cy - ecy) < Math.max(bb.width, eb.width) * 0.45;
          });
          if (!dup) faces.push(f);
        }

        if (faces.length >= 2) {
          state.multiFaceCount++;
          state.noFaceCount = 0;
          setTextCached("piq-proctor-vision", "⚠️ " + faces.length + " people in frame!");
          if (state.multiFaceCount >= 2) {
            reportViolation("MULTIPLE_PERSONS", faces.length + " people detected in camera frame");
            state.multiFaceCount = 0;
          }
          return;
        }
        state.multiFaceCount = 0;

        if (faces.length === 0) {
          state.noFaceCount++;
          state.lookAwayCount = 0;
          setTextCached("piq-proctor-vision", "⚠️ No face visible!");
          if (state.noFaceCount >= 3) {
            reportViolation("NO_FACE", "No face detected — stay centered in camera");
            state.noFaceCount = 0;
          }
          return;
        }
        state.noFaceCount = 0;

        // 1 face detected: check gaze/head orientation (6 BlazeFace keypoints) & rapid movement
        const f = faces[0];
        const bb = f.boundingBox;
        const vw = vid.videoWidth || 320;
        const vh = vid.videoHeight || 240;
        const cx = (bb.originX + bb.width / 2) / vw;
        const cy = (bb.originY + bb.height / 2) / vh;

        let lookingAway = false;
        const kp = f.keypoints || [];
        if (kp.length >= 4) {
          const rEye = kp[0], lEye = kp[1], nose = kp[2], mouth = kp[3];
          const eyeMidX = (rEye.x + lEye.x) / 2;
          const eyeMidY = (rEye.y + lEye.y) / 2;
          const eyeDist = Math.max(Math.abs(lEye.x - rEye.x), 0.03);
          const yawRatio = Math.abs(nose.x - eyeMidX) / eyeDist;
          if (yawRatio > 0.62 || nose.y < eyeMidY - 0.01 || nose.y > mouth.y + 0.01) {
            lookingAway = true;
          }
        }
        if (lookingAway) {
          state.lookAwayCount++;
          setTextCached("piq-proctor-vision", "⚠️ Looking away from screen");
          if (state.lookAwayCount >= 2) {
            reportViolation("LOOKING_AWAY", "Candidate looking away or head turned");
            state.lookAwayCount = 0;
          }
        } else {
          state.lookAwayCount = 0;
        }

        let movedFast = cv.motionFrac > 0.24;
        if (state.lastFaceCenter) {
          const shift = Math.hypot(cx - state.lastFaceCenter.x, cy - state.lastFaceCenter.y);
          if (shift > 0.24) movedFast = true;
        }
        state.lastFaceCenter = { x: cx, y: cy };

        if (movedFast) {
          state.moveCount++;
          if (!lookingAway) setTextCached("piq-proctor-vision", "⚠️ Sudden movement detected");
          if (state.moveCount >= 2) {
            reportViolation("SUSPICIOUS_MOVEMENT", "Excessive human movement detected in frame");
            state.moveCount = 0;
          }
        } else {
          state.moveCount = 0;
          if (!lookingAway) setTextCached("piq-proctor-vision", "Vision AI: 1 face · OK");
        }
      } else {
        // Offline / pre-load Canvas CV fallback
        if (cv.clusters >= 2) {
          state.multiFaceCount++;
          state.noFaceCount = 0;
          setTextCached("piq-proctor-vision", "⚠️ Multiple people detected!");
          if (state.multiFaceCount >= 2) {
            reportViolation("MULTIPLE_PERSONS", "More than one person detected in camera frame");
            state.multiFaceCount = 0;
          }
        } else if (cv.skinFrac < 0.004) {
          state.noFaceCount++;
          state.multiFaceCount = 0;
          setTextCached("piq-proctor-vision", "⚠️ Face not clearly visible");
          if (state.noFaceCount >= 4) {
            reportViolation("NO_FACE", "No face detected in camera frame");
            state.noFaceCount = 0;
          }
        } else if (cv.motionFrac > 0.24) {
          state.moveCount++;
          state.multiFaceCount = 0;
          state.noFaceCount = 0;
          setTextCached("piq-proctor-vision", "⚠️ Excessive movement!");
          if (state.moveCount >= 2) {
            reportViolation("SUSPICIOUS_MOVEMENT", "Suspicious human movement in camera frame");
            state.moveCount = 0;
          }
        } else {
          state.multiFaceCount = 0;
          state.noFaceCount = 0;
          state.moveCount = 0;
          setTextCached("piq-proctor-vision", "Vision CV: Person present");
        }
      }
    } catch (e) {}
  }, 750);

  // 3. Web Audio External Noise Monitor (300ms)
  state.audioTimer = setInterval(function() {
    if (!state.active || !state.armed || !state.analyser || !state.audioData) return;
    if (isVoiceMode && parentWin.__piqAiSpeaking) {
      state.noiseCount = 0;
      return;
    }
    try {
      state.analyser.getByteTimeDomainData(state.audioData);
      let sumSq = 0;
      const arr = state.audioData;
      for (let i = 0; i < arr.length; i++) {
        const v = (arr[i] - 128) / 128;
        sumSq += v * v;
      }
      const rms = Math.sqrt(sumSq / arr.length);
      const threshold = isVoiceMode ? 0.42 : 0.13;
      const neededTicks = isVoiceMode ? 4 : 3;
      if (rms > threshold) {
        state.noiseCount++;
        if (state.noiseCount >= neededTicks) {
          reportViolation("LOUD_NOISE", "External loud noise or background speech detected");
          state.noiseCount = 0;
        }
      } else {
        state.noiseCount = Math.max(0, state.noiseCount - 1);
      }
    } catch (e) {}
  }, 300);

  syncOverlay();
})();
</script>
</body>
</html>"""
    return (
        template.replace("__PAGE_TYPE__", json.dumps(page_type))
        .replace("__MAX_STRIKES__", str(MAX_PROCTOR_STRIKES))
        .replace("__BRIDGE_LABEL__", json.dumps(BRIDGE_KEY))
        .replace("__SESSION_NONCE__", json.dumps(session_nonce))
    )


def clear_proctor_overlay() -> None:
    """Remove any parent-document proctor overlay/monitor and release fullscreen/media after exit/completion."""
    components.html(
        """<script>
        (function(){
          try {
            const p = window.parent || window;
            const d = p.document || document;
            p.__piqAiSpeaking = false;
            try { if (p.speechSynthesis) p.speechSynthesis.cancel(); } catch(e){}
            if (p.__piqProctor) {
              p.__piqProctor.active = false;
              p.__piqProctor.armed = false;
              p.__piqProctor.wasFullscreen = false;
              p.__piqProctor.strikes = 0;
              p.__piqProctor.nonce = "";
              clearInterval(p.__piqProctor.watchTimer);
              clearInterval(p.__piqProctor.visionTimer);
              clearInterval(p.__piqProctor.audioTimer);
              if (p.__piqProctor.stream) {
                try { p.__piqProctor.stream.getTracks().forEach(t => t.stop()); } catch(e){}
                p.__piqProctor.stream = null;
              }
              if (p.__piqProctor.audioCtx && p.__piqProctor.audioCtx.state !== "closed") {
                try { p.__piqProctor.audioCtx.close().catch(()=>{}); } catch(e){}
                p.__piqProctor.audioCtx = null;
                p.__piqProctor.analyser = null;
              }
            }
            ["piq-proctor-lock-overlay", "piq-proctor-pip", "piq-proctor-toast"].forEach(id => {
              const el = d.getElementById(id);
              if (el) el.remove();
            });
            if (p.navigator && p.navigator.keyboard && p.navigator.keyboard.unlock) {
              try { p.navigator.keyboard.unlock(); } catch(e){}
            }
            const exitFs = d.exitFullscreen || d.webkitExitFullscreen || d.mozCancelFullScreen || d.msExitFullscreen;
            const fsEl = d.fullscreenElement || d.webkitFullscreenElement || d.mozFullScreenElement || d.msFullscreenElement;
            if (fsEl && exitFs) {
              Promise.resolve(exitFs.call(d)).catch(()=>{});
            }
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
    st.text_input(BRIDGE_KEY, key=BRIDGE_KEY, label_visibility="collapsed")
    raw_event = st.session_state.get(BRIDGE_KEY) or st.query_params.get("proctor_event")
    if raw_event and raw_event not in _load().seen_events:
        _save(_load().with_event_seen(raw_event))
        parts = str(raw_event).split(":", 2)
        event_type = parts[0]
        detail = parts[2] if len(parts) > 2 else "Proctoring rule violation detected"
        if event_type in STRIKE_EVENT_TYPES:
            record_strike(event_type, detail)
        if st.query_params.get("proctor_event"):
            with contextlib.suppress(Exception):
                del st.query_params["proctor_event"]

    if is_breached() and on_breach:
        on_breach()

    components.html(
        proctor_html_component(page_type, session_nonce=_load().session_nonce),
        height=0,
        width=0,
    )

    return ProctorGuardStatus(
        strikes=get_strikes(),
        is_breached=is_breached(),
        warning=get_warning(),
        flags=get_proctor_flags(),
    )


def render_warning_banner() -> None:
    """Display a clean Streamlit warning banner for strikes, camera warnings, or proctor breach."""
    if is_breached():
        flags = ", ".join(get_proctor_flags()) or FLAG_PROCTOR_BREACH
        st.error(
            f"🚨 **PROCTOR BREACH ({MAX_PROCTOR_STRIKES}/{MAX_PROCTOR_STRIKES} flags):** "
            f"Assessment flagged ({flags}) and force-submitted.",
            icon=":material/gavel:",
        )
    elif get_strikes() > 0:
        strikes = get_strikes()
        flags = ", ".join(get_proctor_flags())
        st.warning(
            f"⚠️ **PROCTOR WARNING:** Flag {strikes}/{MAX_PROCTOR_STRIKES} recorded ({flags}). "
            "Tab/app/desktop switching, gestures, multiple people, looking away, loud noise, or "
            f"exiting fullscreen is prohibited. {MAX_PROCTOR_STRIKES} flags end your assessment.",
            icon=":material/warning:",
        )
    elif warning := get_warning():
        st.warning(f"⚠️ **PROCTOR NOTICE:** {warning}", icon=":material/videocam_off:")
