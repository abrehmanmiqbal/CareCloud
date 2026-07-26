"""
CareCloud Voice Patient Registration Agent — backend
-----------------------------------------------------
Architecture:
  Browser demo → this repo's own mic UI (via /api/chat) ──┐
                                                            ├─► voice_agent.run_agent_turn()
  (Optional, unused in this submission — see README        │        │
   "Telephony" section) Phone call → Vapi Custom LLM  ──────        ▼
   → /chat/completions ─────────────────────────────►  patient_service (validation + DB)
                                                                     │
  REST clients ───────────────────────────────────────────────────►▼
                                    /patients (CRUD)         patients table (SQLite / Postgres)

Every entry point funnels through the SAME service layer (voice_agent →
patient_service → database), so a patient registered by voice follows the
exact same validation and lands in the exact same table as one created
through the REST API directly. The /chat/completions route is kept because
it costs nothing to keep and only needs GROQ_API_KEY like everything else —
but no telephony number is currently attached to it; see README.md for why.

Every entry point funnels through the SAME service layer, so a patient
registered by phone follows the exact same validation and lands in the
exact same table as one created through the REST API directly.
"""
import logging
import os
from pathlib import Path

from fastapi import FastAPI, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from database import init_db
from patients import router as patients_router
import voice_agent as agent

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("carecloud")

BASE_DIR = Path(__file__).parent

app = FastAPI(title="CareCloud Voice Patient Registration Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()
    logger.info(
        "startup complete — groq_key_set=%s db=%s",
        bool(agent.GROQ_API_KEY),
        os.environ.get("DATABASE_URL", "sqlite (local file)")[:40],
    )


# --------------------------------------------------------------------------
# Consistent JSON envelope for every error response: {"data": null, "error": ...}
# --------------------------------------------------------------------------


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"data": None, "error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    errors = {}
    for e in exc.errors():
        field = e["loc"][-1]
        errors[str(field)] = e["msg"]
    return JSONResponse(status_code=422, content={"data": None, "error": errors})


# --------------------------------------------------------------------------
# Patient REST API
# --------------------------------------------------------------------------

app.include_router(patients_router)


# --------------------------------------------------------------------------
# Voice agent endpoints
# --------------------------------------------------------------------------


class ChatTurn(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    history: list[ChatTurn]


class ChatResponse(BaseModel):
    reply: str
    result: dict | None = None


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Used by this repo's own browser demo UI (static/index.html)."""
    messages = [{"role": "system", "content": agent.SYSTEM_PROMPT}] + [
        {"role": t.role, "content": t.content} for t in req.history
    ]
    reply_text, result = await agent.run_agent_turn(messages)
    return ChatResponse(reply=reply_text, result=result)


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    text = await agent.transcribe_audio(
        audio_bytes, audio.filename or "clip.webm", audio.content_type or "audio/webm"
    )
    return {"text": text}


@app.post("/chat/completions")
async def vapi_chat_completions(payload: dict):
    """Vapi-compatible 'Custom LLM' endpoint. Point a Vapi assistant's Custom
    LLM provider base URL at this server, and Vapi handles the real phone
    number, telephony, STT, and TTS — this endpoint stays the exact same
    registration 'brain' used by the browser demo above."""
    incoming = payload.get("messages", [])
    messages = [{"role": "system", "content": agent.SYSTEM_PROMPT}]
    for m in incoming:
        if m.get("role") == "system":
            continue  # skip Vapi's default system message; ours takes priority
        messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    reply_text, _ = await agent.run_agent_turn(messages)
    return agent.vapi_response_envelope(reply_text)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "groq_key_set": bool(agent.GROQ_API_KEY),
        "database": "postgres" if os.environ.get("DATABASE_URL") else "sqlite",
    }


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")