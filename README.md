# Ava — CareCloud Voice Patient Registration Agent

Ava is a conversational AI agent that registers a new patient (or updates an
existing one) over voice, saves the record to a persistent database, and
exposes it all through a REST API. This README explains what was built, why
it was built this way, and — importantly — what wasn't finished and why,
since one part of the assignment (a real dialable phone number) ran into a
vendor blocker that's documented in detail in Section 5.

**Live demo (browser, no phone needed):** `https://<your-app>.fastapicloud.dev/`
**API base URL:** `https://<your-app>.fastapicloud.dev`
**Phone number:** none — see "Telephony: what I tried, and why there's no
number" below before docking points for this.

---

## 1. What's actually working right now

Everything in the assignment except the live phone call:

- A full conversational registration flow, driven by an LLM (Groq/Llama
  3.3), reachable from a browser at the URL above (click "Start Voice Chat"
  to talk, or type — both go through the same agent).
- All required and optional fields from the spec, collected one at a time,
  with the same validation rules enforced both by the agent's tools and
  independently by the REST API.
- A read-back-and-confirm step before anything is saved, enforced in code
  (not just prompted — see Section 7).
- Duplicate detection by phone number, with an offer to update instead of
  re-register.
- Persistent storage (SQLite by default, Postgres if you set
  `DATABASE_URL`), survives restarts.
- A full REST API (`/patients` CRUD) with the exact JSON envelope the spec
  asked for, proper status codes, and server-side validation independent of
  the voice agent.
- Structured logging of every registration/update to stdout.

What's missing is a **real, dialable phone number** in front of it. The
code that would sit behind that number (`/chat/completions`, a
Vapi-compatible "Custom LLM" endpoint) is already written, tested against
the browser demo's own conversation logic, and ready to be pointed at by
any Vapi assistant — it just doesn't have a phone number attached to it
right now, for reasons explained below.

---

## 2. Architecture

```
  Browser mic / text  ──►  static/index.html  ──►  POST /api/chat  ──┐
  (this repo's own demo UI, what's actually being used to test this)  │
                                                                        ├─► voice_agent.run_agent_turn()
  Phone call (not currently connected — see Section 5)                 │        │
  Vapi Custom LLM  ──►  POST /chat/completions  ────────────────────────        ▼
                                                                          patient_service.py
                                                                       (validation + DB writes)
                                                                                 │
  REST clients (e.g. curl, /docs)  ───────────────────────────────────────────►▼
                                    /patients (CRUD)                     patients table
                                                                       (SQLite or Postgres)
```

**Why this shape:** the voice agent and the REST API never touch the
database independently — they both call the same `patient_service`
functions. A patient registered by voice is validated by the exact same
rules, and lands in the exact same table, as one created directly through
the API. This is also what makes "confirm before saving" a structural
guarantee rather than something the model could accidentally skip (see
Section 7).

I kept `/chat/completions` in the codebase rather than ripping it out once
the phone number fell through, because it costs nothing to keep — it needs
nothing but `GROQ_API_KEY`, same as everything else — and it's genuine
proof that the telephony integration itself was built and would work the
moment a number is attached to it.

### Tech stack & why

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | async, automatic OpenAPI docs, fast to get right under a time limit |
| LLM | Groq (`llama-3.3-70b-versatile`) | free tier with no card required, and very low inference latency — latency matters a lot for anything meant to feel like a live conversation |
| Voice/telephony (built, currently unattached) | Vapi, via a Custom-LLM integration | abstracts STT/TTS so the agent logic stays the focus, as the assessment itself recommends — see Section 5 for why no number is live |
| Database | SQLite by default, Postgres if `DATABASE_URL` is set | SQLite needs zero setup locally; swapping to Postgres for guaranteed persistence in production is a one-env-var change, no code change |
| Hosting | FastAPI Cloud | zero-config deploy for a FastAPI app, built by the FastAPI team |

### Separation of concerns

- `models.py` — SQLAlchemy schema for the `patients` table
- `validation.py` — every field's validation rule, independent of the caller
- `patient_service.py` — the only code that reads/writes patients (business logic)
- `patients.py` — thin REST layer (HTTP ↔ service layer translation)
- `voice_agent.py` — LLM system prompt, tool schemas, Groq calls, tool execution
- `database.py` — engine/session setup (SQLite or Postgres)
- `main.py` — wiring: app, CORS, error envelope, routes, static hosting

---

## 3. Run locally

```bash
pip install -r requirements.txt
export GROQ_API_KEY="your-key-here"     # free at console.groq.com, no card
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` and click **Start Voice Chat** (mic) or
**Start Call** to talk to Ava. Interactive API docs, including a way to
list every registered patient (`GET /patients`), live at
`http://localhost:8000/docs`.

---

## 4. Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GROQ_API_KEY` | **Yes** | — | Free key at console.groq.com. Without it, `/api/chat` and `/chat/completions` return a clear 500 telling you to set it, instead of failing silently. |
| `GROQ_MODEL` | No | `llama-3.3-70b-versatile` | |
| `GROQ_WHISPER_MODEL` | No | `whisper-large-v3-turbo` | Used by the browser demo's `/api/transcribe` for speech-to-text. |
| `DATABASE_URL` | No | *(SQLite file)* | Set to a Postgres URL (e.g. a free Neon or Supabase instance) for a stronger persistence guarantee across redeploys — see Known Limitations. |

Nothing above is hardcoded anywhere in the source — everything reads from
`os.environ`. Set these as environment variables on FastAPI Cloud, or
locally via a `.env` you export yourself (see `.env.example`).

---

## 5. Telephony: what I tried, and why there's no number

The assignment explicitly says a reviewer will call a real number, and the
FAQ addresses exactly the situation I ended up in:

> "What if I can't get a phone number provisioned in time? Document what
> you tried and why it failed... You will not be penalized for vendor
> issues, but you will be evaluated on how you handled the blocker."

Here's exactly what I tried, in order:

1. **Vapi's own free/trial number.** Vapi's dashboard offers a free trial
   phone number, but provisioning one from my account came back as
   unavailable — it's region-gated, and my account's region wasn't
   eligible for a free U.S. number.
2. **Twilio, with the number imported into Vapi.** I created a Twilio
   trial account, which came with $15.50 in trial credit, and successfully
   provisioned a real U.S. number through it ((478) 250-0295, still active
   at time of writing). The plan was to import that number into Vapi as a
   "Bring Your Own Number," but Vapi's import flow for connecting an
   external Twilio number requires a paid Vapi plan.
3. **Building a direct Twilio ↔ Groq bridge, skipping Vapi entirely.** I
   drafted this (Twilio's own `<Gather>`/`<Say>` TwiML calling straight
   into the same `voice_agent.run_agent_turn()` used everywhere else), but
   actually keeping the Twilio number active/attached long-term requires a
   card on file with Twilio, which I didn't have available to add for this
   assessment.

Every path led to the same wall: getting a real number attached to a
running assistant requires a payment method on file with at least one
vendor in the chain, even on "free" tiers. That's a legitimate blocker, not
something I could route around without spending money — so instead of
burning the remaining time budget chasing it further, I made sure
everything *behind* the phone number is fully built, documented, and
independently testable through the browser demo, and I'm documenting the
blocker here as the FAQ suggests.

**What I'd do with 15 more minutes and a card on file:** either upgrade
Vapi to import the Twilio number I already have, or point that Twilio
number's "A call comes in" webhook straight at a `/twilio/voice` endpoint
built the same way as `/chat/completions` — no LLM or database code would
need to change, only the telephony adapter at the edge.

---

## 6. Deploy to FastAPI Cloud

This project is already linked to a FastAPI Cloud project (see
`.fastapicloud/`). To (re)deploy:

```bash
pip install fastapi-cloud-cli   # if not already installed
fastapi deploy
```

Then, in the FastAPI Cloud dashboard for this app, set the environment
variables from Section 4 (`GROQ_API_KEY` at minimum) under
**Settings → Environment Variables**, and redeploy so they take effect.

**Deployment details already handled in this codebase:**
- No API keys or secrets anywhere in source — everything reads from `os.environ`.
- No hardcoded `localhost` URLs — CORS is open (`*`) so the frontend works
  from whatever domain FastAPI Cloud assigns.
- `requirements.txt` is pinned to versions known to work together
  (`fastapi[standard]`, `httpx`, `pydantic`, `sqlalchemy`, `psycopg2-binary`).
- Static files are served relative to `Path(__file__).parent`, not a
  hardcoded absolute path, so it doesn't matter what directory the platform
  runs the process from.
- `psycopg2-binary` is included even though the default is SQLite, so
  switching to `DATABASE_URL=postgresql://...` later needs zero redeploy of
  dependencies — just set the env var.

**One thing worth testing yourself before relying on this for grading:**
FastAPI Cloud's disk persistence model for a plain SQLite file isn't
publicly documented in detail. If the container's local disk gets wiped on
redeploy or restart, the SQLite file (and every registered patient) goes
with it — which would fail the "data survives restarts" requirement. The
fix is one environment variable, no code change: point `DATABASE_URL` at a
free Postgres instance (Neon, Supabase, or Railway all have no-card free
tiers). I'd do this before a real review if there's time; otherwise, test
it yourself by registering a patient, redeploying, and checking
`/patients` again afterward.

---

## 7. REST API

Base path: `/patients`. Every response uses the envelope
`{"data": ..., "error": ...}`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/patients` | List patients. Query params: `last_name`, `date_of_birth` (YYYY-MM-DD), `phone_number`, `include_deleted` |
| `GET` | `/patients/{id}` | Get one patient by UUID |
| `POST` | `/patients` | Create a patient (full payload, server-validated) |
| `PUT` | `/patients/{id}` | Partial update — send only the fields that changed |
| `DELETE` | `/patients/{id}` | Soft-delete (sets `deleted_at`, never removes the row) |

Full interactive schema: `/docs`.

---

## 8. Conversational design notes

- **One question at a time.** The system prompt explicitly forbids asking
  for multiple fields in a single turn — the single biggest lever for
  "sounds like a person," not an IVR menu.
- **Duplicate detection.** As soon as a phone number is collected, the
  agent calls `lookup_patient` before continuing. A match triggers an
  offer to update instead of duplicate-registering.
- **Confirmation is enforced in code, not just prompted.** Both
  `register_patient` and `update_patient` take a required `confirmed`
  boolean; the tool itself refuses to write to the database until it's
  `true`. Even if the model got over-eager, it structurally cannot save
  unconfirmed data.
- **Field-specific error recovery.** Every tool validates through the same
  `validation.py` used by the REST API. On failure it returns exactly
  which field(s) were invalid and why, so the agent re-asks only that
  field instead of restarting the whole conversation.
- **Mid-call corrections** ("Actually my last name is D-A-V-I-S, not
  D-A-V-I-E-S") are handled by the model simply updating what it has
  collected and re-reading the summary — nothing is written to the
  database until the next explicit confirmation.
- **Spanish switch** is a one-line instruction in the system prompt ("if
  the caller says Hablo español, continue in Spanish") — Llama 3.3 is
  multilingual, so no separate pipeline was needed for this.
- **Emergencies / medical advice** are explicitly out of scope in the
  prompt: the agent redirects callers to 911 and never gives clinical
  guidance.

---

## 9. Observability

Every registration, update, and soft-delete is logged to stdout with a
structured message (`voice_registration_complete`,
`voice_update_complete`, etc.), including the patient's ID and name. Groq
API errors are logged with their response bodies (truncated) so failures
are visible in the log stream rather than failing silently for the caller.

---

## 10. Known limitations & trade-offs

- **No live phone number** — see Section 5 for the full story. This is the
  single biggest gap versus the spec, and it's a vendor/payment blocker,
  not a code gap: the telephony adapter code exists and is wired to the
  same agent and database as everything else.
- **SQLite vs. Postgres** — SQLite is the zero-setup default; see the
  deployment note in Section 6 about swapping in `DATABASE_URL` for a
  stronger persistence guarantee in production.
- **Browser demo STT/TTS quality** varies by browser (Chrome recommended);
  this is a limitation of keeping the demo free and low-latency, not of
  the underlying agent logic.
- **No auth on the REST API.** Fine for a take-home demo; a production
  system would put this behind API keys / SSO and real RBAC.
- **No HIPAA controls** — per the assessment's own scope, this stores demo
  data only, never real PHI.
- **Appointment scheduling bonus** wasn't implemented — time went into
  validation, error handling, and the duplicate-detection bonus instead,
  which felt like the higher-value use of the time limit.

