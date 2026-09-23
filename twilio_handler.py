"""
Telephony layer — Twilio voice webhooks
----------------------------------------
Flow:
  Caller dials +1 (470) 256-6802
    → Twilio POSTs /twilio/voice          → TwiML: <Gather input="speech"><Say>greeting</Say></Gather>
    → Twilio POSTs /twilio/gather         → SpeechResult → voice_agent.run_agent_turn()
                                            → fresh TwiML: <Say>reply</Say> + next <Gather>
    → Twilio POSTs /twilio/status (end)   → session cleanup

Design notes:
  * Twilio's built-in speech recognition (phone_call model) and Polly TTS handle
    STT/TTS — no Deepgram/ElevenLabs/Vapi needed.
  * Conversation state is kept in memory, keyed by Twilio CallSid. The app runs
    as a single worker on FastAPI Cloud, so an in-memory dict is correct and
    fast. Data safety does NOT depend on it: the patient record is written by
    the service layer (same one the REST API uses) BEFORE the call hangs up.
    If the process restarts mid-call, the caller hears a graceful apology and
    the call ends — never silence.
  * <Say> is nested INSIDE <Gather> so callers can barge in while Ava speaks.
  * On a completed registration/update, the call says the closing line and
    <Hangup/> — the LLM never ends the call silently.
"""
import logging
import time
from xml.sax.saxutils import escape

from fastapi import APIRouter, Form, Response

import voice_agent as agent

logger = logging.getLogger("carecloud.twilio")

router = APIRouter(prefix="/twilio", tags=["twilio"])

# ---------------------------------------------------------------------------
# In-memory call sessions: CallSid -> {"messages": [...], "last_active": ts}
# ---------------------------------------------------------------------------
SESSION_TTL_SECONDS = 4 * 3600
_sessions: dict[str, dict] = {}


def _sweep():
    now = time.time()
    for sid in [s for s, v in _sessions.items() if now - v["last_active"] > SESSION_TTL_SECONDS]:
        _sessions.pop(sid, None)


def _get_messages(call_sid: str) -> list:
    session = _sessions.get(call_sid)
    if session is None:
        session = {"messages": [{"role": "system", "content": agent.SYSTEM_PROMPT}], "last_active": time.time()}
        _sessions[call_sid] = session
    return session["messages"]


# ---------------------------------------------------------------------------
# TwiML builders
# ---------------------------------------------------------------------------

def _gather_inner(say_text: str) -> str:
    """<Say> nested in <Gather>: caller can speak over Ava and still be heard."""
    return (
        '<Gather input="speech" action="/twilio/gather" method="POST" '
        'speechTimeout="auto" speechModel="phone_call" language="en-US" timeout="8">'
        f'<Say voice="Polly.Joanna">{escape(say_text)}</Say>'
        "</Gather>"
    )


def _xml(inner: str) -> Response:
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response>' + inner + "</Response>",
        media_type="application/xml",
    )


GREETING = (
    "Thanks for calling CareCloud. This is Ava, your virtual patient "
    "registration assistant. Can I get your first and last name to get started?"
)

GOODBYE_SILENCE = (
    "I didn't hear anything, so I'll let you go. Feel free to call back any "
    "time. Goodbye."
)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@router.post("/voice")
async def incoming_call(CallSid: str = Form(...), From: str = Form(default="")):
    """Twilio 'A call comes in' webhook. Fresh call → fresh session."""
    _sessions.pop(CallSid, None)
    _sweep()
    logger.info("twilio_call_started call_sid=%s from=%s", CallSid, From)
    return _xml(_gather_inner(GREETING))


@router.post("/gather")
async def gather(
    CallSid: str = Form(...),
    SpeechResult: str = Form(default=""),
    Confidence: float = Form(default=0.0),
):
    """Each caller utterance lands here. Run the shared agent, reply with the
    next TwiML <Say>/<Gather> (or <Hangup/> after a successful save)."""
    text = (SpeechResult or "").strip()

    if not text:
        # Gather timed out with no speech — give one nudge, then end gracefully.
        return _xml(_gather_inner("Are you still there? Whenever you're ready, just speak.") +
                    f'<Say voice="Polly.Joanna">{escape(GOODBYE_SILENCE)}</Say><Hangup/>')

    messages = _get_messages(CallSid)
    messages.append({"role": "user", "content": text})
    logger.info("twilio_turn call_sid=%s confidence=%.2f speech=%r", CallSid, Confidence, text[:120])

    try:
        reply, result = await agent.run_agent_turn(messages)
    except Exception as exc:  # Groq quota/down, DB failure, anything — never silence
        logger.exception("twilio_agent_turn_failed call_sid=%s", CallSid)
        reply = ("I'm sorry, I'm having a technical problem right now and couldn't "
                 "finish your registration. Please call back in a few minutes.")
        result = None
        messages.append({"role": "assistant", "content": reply})
        return _xml(f'<Say voice="Polly.Joanna">{escape(reply)}</Say><Hangup/>')

    messages.append({"role": "assistant", "content": reply})
    _sessions[CallSid]["last_active"] = time.time()

    # A registration/update just persisted → say the LLM's closing line, then hang up.
    hangup = result is not None
    inner = f'<Say voice="Polly.Joanna">{escape(reply)}</Say>'
    if hangup:
        logger.info("twilio_registration_complete call_sid=%s type=%s patient_id=%s",
                    CallSid, result.get("type"), result.get("patient_id"))
        inner += "<Hangup/>"
    else:
        inner = _gather_inner(reply)
    return _xml(inner)


@router.post("/status")
async def call_status(CallSid: str = Form(...), CallStatus: str = Form(default="")):
    """Twilio 'Call status changes' webhook — frees the session when the call ends."""
    _sessions.pop(CallSid, None)
    logger.info("twilio_call_ended call_sid=%s status=%s", CallSid, CallStatus)
    return _xml("")