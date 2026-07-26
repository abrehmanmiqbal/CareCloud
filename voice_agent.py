"""
Voice agent — the conversational "brain" for patient registration.

Shared by:
  - /api/chat            (browser demo — mic in this repo's own UI)
  - /chat/completions     (Vapi "Custom LLM" endpoint — real phone number)

Both entry points funnel every turn through run_agent_turn(), which calls
Groq's LLM, executes any tool call against patient_service (the same layer
the REST API uses), and returns plain text ready to be spoken back to the
caller.
"""
import json
import logging
import os
import time
import uuid

import httpx
from fastapi import HTTPException

from database import SessionLocal
import patient_service as svc

logger = logging.getLogger("carecloud.voice_agent")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_WHISPER_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")

# --------------------------------------------------------------------------
# System prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Ava, CareCloud's AI patient registration assistant. You handle phone
calls that register new patients, and you speak like a warm, efficient, professional intake
coordinator — never robotic or scripted.

VOICE STYLE
- Keep every reply SHORT: 1-3 sentences. This is a live phone call, not a chat window.
- No markdown, no bullet lists, no emojis — nothing odd when read aloud by text-to-speech.
- Ask for ONE piece of information at a time. Never ask for three fields in one breath.
- If the caller says "Hablo español" or otherwise addresses you in Spanish, continue the rest of
  the call in Spanish.

REQUIRED FIELDS (collect these first, one at a time, in a natural order):
first_name, last_name, date_of_birth, sex (Male, Female, Other, or Decline to Answer),
phone_number, address_line_1, city, state, zip_code.
- Normalize date_of_birth to YYYY-MM-DD before using it in any tool call, no matter how the
  caller says it ("March 3rd 1990", "3/3/90", etc).
- Normalize phone numbers to 10 digits before using them in a tool call.

DUPLICATE CHECK
- As soon as you have the phone number, call lookup_patient with it.
- If a match is found, tell the caller: "It looks like we already have a record for [First]
  [Last]. Would you like to update your information instead?" If they say yes, switch to
  collecting only the fields they want to change, then use update_patient (not register_patient).
  If they say no, continue registering fresh.

OPTIONAL FIELDS
- Once all required fields are collected, offer once: "I can also collect your insurance
  information, emergency contact, and preferred language — would you like to add any of that?"
  Only collect what they opt into. Don't push.

CONFIRMATION (mandatory before saving)
- Before calling register_patient or update_patient, read back everything you collected in one
  natural summary sentence and ask the caller to confirm or correct anything.
- Only call register_patient / update_patient with confirmed=true after the caller has
  explicitly agreed. If they correct something, update your understanding and read the summary
  back again before proceeding — do not save on a correction turn.

ERROR HANDLING
- If a tool call returns a validation_error, apologize briefly and re-ask ONLY the specific
  field(s) that were flagged — don't restart the whole conversation.
- If the caller wants to start over, acknowledge it warmly and begin collecting from scratch.
- If a tool call fails or returns an unexpected error, tell the caller there was a technical
  problem saving their information and that they may need to try again shortly — never go
  silent, never pretend it worked.

CALL COMPLETION
- After a successful registration or update, give a brief, warm confirmation using their first
  name (e.g. "You're all set, Maria — you're registered with us.") and let the call end.

BOUNDARIES
- Never give medical advice. If asked a clinical question, say a nurse or provider will follow
  up, and offer to note it.
- This is a demo system, not a real clinic. If a caller describes a real emergency, tell them to
  hang up and dial 911 (or their local emergency number) immediately.
"""

# --------------------------------------------------------------------------
# Tool schemas
# --------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_patient",
            "description": (
                "Look up an existing patient by phone number to check for a duplicate before "
                "registering. Call this as soon as you have the caller's phone number."
            ),
            "parameters": {
                "type": "object",
                "properties": {"phone_number": {"type": "string"}},
                "required": ["phone_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_patient",
            "description": (
                "Create a new patient record. You MUST call this once with confirmed=false is "
                "never necessary — only call it after reading the full summary back to the "
                "caller and receiving explicit confirmation, at which point call with "
                "confirmed=true. If any field is invalid, the tool returns a validation_error "
                "with the specific field(s) to re-ask."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "description": "Must be true. Only set true after the caller has explicitly confirmed the read-back summary.",
                    },
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
                    "sex": {"type": "string", "enum": ["Male", "Female", "Other", "Decline to Answer"]},
                    "phone_number": {"type": "string"},
                    "email": {"type": "string"},
                    "address_line_1": {"type": "string"},
                    "address_line_2": {"type": "string"},
                    "city": {"type": "string"},
                    "state": {"type": "string", "description": "2-letter abbreviation"},
                    "zip_code": {"type": "string"},
                    "insurance_provider": {"type": "string"},
                    "insurance_member_id": {"type": "string"},
                    "preferred_language": {"type": "string"},
                    "emergency_contact_name": {"type": "string"},
                    "emergency_contact_phone": {"type": "string"},
                },
                "required": [
                    "confirmed", "first_name", "last_name", "date_of_birth", "sex",
                    "phone_number", "address_line_1", "city", "state", "zip_code",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_patient",
            "description": (
                "Update fields on an existing patient (found via lookup_patient). Only include "
                "the fields that are changing. Only call with confirmed=true after reading the "
                "changes back to the caller and receiving explicit confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                    "confirmed": {"type": "boolean"},
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "date_of_birth": {"type": "string"},
                    "sex": {"type": "string"},
                    "phone_number": {"type": "string"},
                    "email": {"type": "string"},
                    "address_line_1": {"type": "string"},
                    "address_line_2": {"type": "string"},
                    "city": {"type": "string"},
                    "state": {"type": "string"},
                    "zip_code": {"type": "string"},
                    "insurance_provider": {"type": "string"},
                    "insurance_member_id": {"type": "string"},
                    "preferred_language": {"type": "string"},
                    "emergency_contact_name": {"type": "string"},
                    "emergency_contact_phone": {"type": "string"},
                },
                "required": ["patient_id", "confirmed"],
            },
        },
    },
]

# --------------------------------------------------------------------------
# Groq calls
# --------------------------------------------------------------------------


async def call_groq(messages, tools=None):
    if not GROQ_API_KEY:
        raise HTTPException(
            500,
            "GROQ_API_KEY is not set on the server. Get a free key at console.groq.com "
            "(no card required) and set it as an environment variable.",
        )
    payload = {"model": GROQ_MODEL, "messages": messages, "temperature": 0.4}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code != 200:
        logger.error("groq_error status=%s body=%s", r.status_code, r.text[:500])
        raise HTTPException(502, f"Groq API error: {r.text[:300]}")
    return r.json()


async def transcribe_audio(audio_bytes: bytes, filename: str, content_type: str) -> str:
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY is not set on the server.")
    if len(audio_bytes) < 800:
        return ""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                GROQ_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (filename, audio_bytes, content_type)},
                data={"model": GROQ_WHISPER_MODEL},
            )
    except httpx.TimeoutException as e:
        logger.error("groq_transcribe_timeout %r", e)
        raise HTTPException(504, "Transcription request to Groq timed out.")
    except httpx.HTTPError as e:
        logger.error("groq_transcribe_network_error %r", e)
        raise HTTPException(502, f"Could not reach Groq: {e}")

    if r.status_code != 200:
        logger.error("groq_transcribe_error status=%s body=%s", r.status_code, r.text[:500])
        raise HTTPException(502, f"Groq transcription error ({r.status_code}): {r.text[:300]}")
    return r.json().get("text", "").strip()


# --------------------------------------------------------------------------
# Tool execution — talks to patient_service, the same layer the REST API uses
# --------------------------------------------------------------------------


def _execute_tool(fn_name: str, args: dict) -> dict:
    db = SessionLocal()
    try:
        if fn_name == "lookup_patient":
            patient = svc.find_by_phone(db, args.get("phone_number", ""))
            if not patient:
                return {"found": False}
            p = svc.serialize(patient)
            return {"found": True, "patient": p}

        if fn_name == "register_patient":
            if not args.get("confirmed"):
                return {"status": "confirmation_required", "message": "Read the full summary back to the caller and call again with confirmed=true only after they agree."}
            data = {k: v for k, v in args.items() if k != "confirmed"}
            patient, errors = svc.create_patient(db, data)
            if errors:
                return {"status": "validation_error", "errors": errors}
            logger.info("voice_registration_complete payload=%s", json.dumps(svc.serialize(patient)))
            return {"status": "success", "patient_id": patient.patient_id, "first_name": patient.first_name, "last_name": patient.last_name}

        if fn_name == "update_patient":
            if not args.get("confirmed"):
                return {"status": "confirmation_required", "message": "Read the changes back to the caller and call again with confirmed=true only after they agree."}
            patient_id = args.get("patient_id", "")
            data = {k: v for k, v in args.items() if k not in ("confirmed", "patient_id")}
            patient, errors = svc.update_patient(db, patient_id, data)
            if errors:
                status = "not_found" if set(errors.keys()) == {"patient_id"} else "validation_error"
                return {"status": status, "errors": errors}
            logger.info("voice_update_complete payload=%s", json.dumps(svc.serialize(patient)))
            return {"status": "success", "patient_id": patient.patient_id, "first_name": patient.first_name, "last_name": patient.last_name}

        return {"status": "unknown_tool"}
    except Exception as exc:  # never let a DB/tool error go silent to the caller
        logger.exception("tool_execution_failed tool=%s", fn_name)
        return {"status": "error", "message": "Something went wrong saving that. Please ask the caller to try again shortly."}
    finally:
        db.close()


async def run_agent_turn(messages: list) -> tuple[str, dict | None]:
    """Runs one full agent turn: calls Groq, executes any tool calls, and
    returns (reply_text, result) where result is a small summary dict if a
    registration/update completed successfully this turn (used by the
    browser demo to show a confirmation card), else None."""
    data = await call_groq(messages, tools=TOOLS)
    choice = data["choices"][0]["message"]

    tool_calls = choice.get("tool_calls") or []
    result_summary = None
    if tool_calls:
        messages.append(choice)
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            result = _execute_tool(fn_name, args)
            if fn_name in ("register_patient", "update_patient") and result.get("status") == "success":
                result_summary = {"type": fn_name, **result}
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result)})

        follow_up = await call_groq(messages)
        reply_text = follow_up["choices"][0]["message"]["content"] or ""
    else:
        reply_text = choice.get("content", "") or ""

    return reply_text, result_summary


def vapi_response_envelope(reply_text: str) -> dict:
    """Wraps a reply as an OpenAI-compatible chat.completion, the shape
    Vapi's Custom LLM provider expects."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": GROQ_MODEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": reply_text}, "finish_reason": "stop"}
        ],
    }
