# CareCloud — Voice AI Patient Registration Agent

A voice-based AI agent on a **real, dialable U.S. phone number** that collects standard patient demographics through natural conversation, persists them to a database, and exposes them through a REST API and a web dashboard.

* **Phone:** +1 (470) 256-6802 (live, Twilio) — speak naturally to register a patient
* **API:** https://healthcare-assistant.fastapicloud.dev/patients
* **Dashboard:** https://healthcare-assistant.fastapicloud.dev/dashboard
* **Browser demo:** https://healthcare-assistant.fastapicloud.dev/

---

## Features

* Natural, conversational intake ("Ava") — not a rigid IVR menu
* One question at a time, but handles multiple fields in a single answer
* Graceful corrections ("Actually my last name is D-A-V-I-S"), start-over requests, interruptions
* Re-prompts **only** the invalid/missing field, never the whole form
* Optional fields offered once, collected only if the caller opts in
* Full read-back of everything collected before saving, then **explicit confirmation required**
* Duplicate detection by phone number — offers to update an existing record instead
* Same agent logic serves the phone line and the browser demo
* REST API with consistent envelope, proper status codes, server-side validation, soft deletes
* Web dashboard: list, search, detail view, soft delete
* Automated test suite (`test_api.py`)

## Architecture

```text
Caller ──phone──► Twilio ──webhook──► /twilio/voice · /twilio/gather
                                       (twilio_handler.py: TwiML <Gather input="speech">,
                                        per-CallSid session in memory)
Browser ──mic──► /api/chat · /api/transcribe ──┐
                                                ├─► voice_agent.run_agent_turn()
REST clients ─────────► /patients ──────────────┘        │
                                                            ▼
                                              patient_service.py (validation,
                                              duplicate check, confirmed gate)
                                                            │
                                                            ▼
                                              SQLite file  ──or── Postgres (DATABASE_URL)
```

**Key rule:** The LLM never writes to the database. Every save flows:

conversation → structured patient state → server-side validation → read-back →
explicit confirmation (`confirmed: true`, **enforced in code** in
`voice_agent._execute_tool` — a tool call without it returns
`confirmation_required` and writes nothing) → `patient_service` → DB → the
outcome is spoken back to the caller.

A failed DB write is reported to the caller, never silently swallowed.

Both entry points (phone and browser) call the identical
`voice_agent.run_agent_turn()`, and both it and the REST API call the same
`patient_service` layer — so a patient registered by voice passes the exact
same validation and lands in the same table as one created via `POST /patients`.

## Tech stack & why

| Layer     | Choice                                         | Why                                                                                                                        |
| --------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Telephony | Twilio + `<Gather input="speech">` + Polly TTS | Built-in phone-grade STT/TTS — no Deepgram/ElevenLabs/Vapi accounts or per-minute bills; the fastest path to a real number |
| LLM       | Groq — `llama-3.3-70b-versatile`               | Fast, generous free tier, OpenAI-compatible API                                                                            |
| Backend   | FastAPI + SQLAlchemy                           | Typed, async, tiny footprint; the assessment's recommended Python stack                                                    |
| Database  | SQLite locally / Postgres via `DATABASE_URL`   | SQLite = zero-setup local dev; DATABASE_URL swap removes any doubt about ephemeral deploy disks                            |
| Hosting   | FastAPI Cloud                                  | One-command deploys; app is already linked (`.fastapicloud/`)                                                              |

## Voice agent conversational design

The full system prompt is in `voice_agent.py` (`SYSTEM_PROMPT`) with inline comments. Design decisions:

* **Short replies (1–3 sentences), no markdown** — it's read aloud by TTS.
* **One field at a time** in a natural order; the model is explicitly allowed to accept multi-field answers when offered.
* **Normalization before tool calls:** dates → `YYYY-MM-DD`, phones → 10 digits.
* **Duplicate check** as soon as the phone number is known (`lookup_patient`).
* **Optional fields offered once**, phrased per the assessment brief.
* **Confirmation gate:** read back everything, get explicit agreement, then call the save tool once with `confirmed=true`. A correction resets the read-back.
* **Error style:** apologize briefly, re-ask only the flagged field(s).
* **Boundaries:** no medical advice; real emergencies → hang up and dial 911.
* **Spanish support:** if the caller switches to Spanish, the agent follows.

### Phone session state

In-memory dict keyed by Twilio `CallSid` (single-worker deployment). Only conversation context lives there — the patient record is persisted before hangup, so a server restart can lose at most the tail of a conversation, and the caller hears a graceful apology rather than silence.

Expired sessions are swept after 4 hours. This is a documented trade-off (see Known Limitations).

## API

Base URL: `https://healthcare-assistant.fastapicloud.dev`

All responses use the envelope `{"data": ..., "error": null}`.

Errors are human-readable strings or `{field: message}` maps — never stack traces.

| Method | Endpoint         | Description                                                                                                    |
| ------ | ---------------- | -------------------------------------------------------------------------------------------------------------- |
| GET    | `/patients`      | List patients. Optional: `?last_name=`, `?date_of_birth=YYYY-MM-DD`, `?phone_number=`, `?include_deleted=true` |
| GET    | `/patients/{id}` | One patient by UUID                                                                                            |
| POST   | `/patients`      | Create (201)                                                                                                   |
| PUT    | `/patients/{id}` | Partial update                                                                                                 |
| DELETE | `/patients/{id}` | Soft delete (sets `deleted_at`)                                                                                |
| GET    | `/api/health`    | Liveness + config sanity                                                                                       |

### Example

```bash
curl -X POST https://healthcare-assistant.fastapicloud.dev/patients \
  -H "Content-Type: application/json" \
  -d '{"first_name":"Jane","last_name":"Doe","date_of_birth":"1985-04-12",
       "sex":"Female","phone_number":"4045550100","address_line_1":"1200 Peachtree St NE",
       "city":"Atlanta","state":"GA","zip_code":"30309"}'
```

Expected response:

```text
201 {"data":{"patient_id":"…","created_at":"…",…},"error":null}
```

### Validation

Validation is enforced server-side on every create/update, independently of the voice agent.

| Field                   | Rule                                           |
| ----------------------- | ---------------------------------------------- |
| first_name / last_name  | 1–50 chars, letters, hyphens, apostrophes      |
| date_of_birth           | Valid date, not in the future                  |
| sex                     | Male / Female / Other / Decline to Answer      |
| phone_number            | U.S. 10-digit (normalized from common formats) |
| email                   | Valid email format (optional)                  |
| state                   | Valid 2-letter U.S. abbreviation               |
| zip_code                | 5-digit or ZIP+4                               |
| emergency_contact_phone | 10-digit if provided                           |

## Database & environment variables

Schema: `patients` table with UUID `patient_id`, demographic fields, `preferred_language` (default English), `created_at` / `updated_at` (UTC), and `deleted_at` for soft deletes.

SQLAlchemy creates the schema automatically at startup.

| Env var              | Required                  | Notes                                                                        |
| -------------------- | ------------------------- | ---------------------------------------------------------------------------- |
| `GROQ_API_KEY`       | **Yes**                   | Free key from Groq Console                                                   |
| `DATABASE_URL`       | Recommended in production | PostgreSQL connection string, e.g. `postgresql+psycopg2://user:pass@host/db` |
| `GROQ_MODEL`         | No                        | Default `llama-3.3-70b-versatile`                                            |
| `CORS_ORIGINS`       | No                        | Comma-separated allowlist; default `*`                                       |
| `SEED_DEMO_PATIENTS` | No                        | Set `0` to skip demo seeding                                                 |

## Local setup

### Windows CMD

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set GROQ_API_KEY=YOUR_GROQ_API_KEY
uvicorn main:app --reload
```

Then open:

```text
http://localhost:8000
http://localhost:8000/dashboard
```

### Linux / macOS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GROQ_API_KEY=YOUR_GROQ_API_KEY
uvicorn main:app --reload
```

## Testing

```bash
SEED_DEMO_PATIENTS=0 DATABASE_URL=sqlite:////tmp/carecloud_test.db pytest test_api.py -v
```

The test suite covers:

* Create, retrieve, list and filter patients
* Partial updates
* Soft deletes
* Validation rules
* Duplicate detection
* Agent confirmation gate
* Error response envelopes

### Testing the voice agent

**By phone:** dial **+1 (470) 256-6802**.

> **Twilio trial account:** if the account is still on trial, the caller's phone number must first be added under **Verified Caller IDs** in the Twilio Console.

**By browser:** open the app and use the browser voice interface.

**By API:** script a registration against:

```text
POST /patients
```

or replay a conversation through:

```text
POST /api/chat
```

## Deployment — FastAPI Cloud

The project is linked through:

```text
.fastapicloud/cloud.json
```

Deploy using the FastAPI Cloud CLI:

```bash
pip install fastapi-cloud
fastapi cloud deploy
```

After deployment, confirm the required environment variables in FastAPI Cloud:

```text
GROQ_API_KEY
```

For production persistence, configure:

```text
DATABASE_URL
```

with a managed PostgreSQL database.

## Twilio configuration

The live Twilio number is:

```text
+1 (470) 256-6802
```

Configure the number in the Twilio Console:

### Incoming call webhook

**Voice Configuration → A call comes in**

Method:

```text
HTTP POST
```

URL:

```text
https://healthcare-assistant.fastapicloud.dev/twilio/voice
```

### Call status webhook

Optional:

```text
https://healthcare-assistant.fastapicloud.dev/twilio/status
```

Method:

```text
HTTP POST
```

The status endpoint allows the application to clean up call sessions after the call ends.

### Twilio trial restriction

On a Twilio trial account, incoming calls may be restricted to verified caller numbers.

To test the live number:

1. Open Twilio Console.
2. Go to **Phone Numbers → Manage → Verified Caller IDs**.
3. Add the phone number you will call from.
4. Complete the verification.
5. Call **+1 (470) 256-6802**.

This is a Twilio account restriction rather than an application-code limitation.

## Dashboard

The dashboard is available at:

```text
https://healthcare-assistant.fastapicloud.dev/dashboard
```

It provides:

* Patient list
* Search
* Name, DOB, phone and city information
* Registration timestamp
* Full patient detail view
* Soft-delete functionality
* Loading, empty and error states

## Observability

The application logs:

* Completed voice registrations
* Final collected patient payloads
* API write operations
* Twilio call lifecycle events
* Twilio `CallSid` values

API keys and other secrets are not logged.

## Security & data handling

* The LLM does not directly write to the database.
* Patient data passes through server-side validation before persistence.
* Explicit patient confirmation is required before saving through the voice agent.
* Soft deletes preserve records rather than physically removing them.
* API errors do not expose stack traces.
* Secrets are supplied through environment variables rather than hard-coded in source files.

## Known limitations

* **Twilio trial restrictions:** trial accounts can require Verified Caller IDs for testing.
* **Phone session state:** conversation state is held in memory and is associated with the Twilio `CallSid`.
* **Single-worker assumption:** the in-memory phone session design is intended for the current deployment architecture.
* **SQLite persistence:** when `DATABASE_URL` is not configured, the application falls back to SQLite. Production deployments should use managed PostgreSQL for durable persistence.
* **Authentication:** the current demo API does not implement production-grade user authentication.
* **Healthcare compliance:** this project is an assessment/demo system and does not implement the complete security, privacy, auditing, access-control, and compliance requirements of a production HIPAA environment.

## Project structure

```text
CareCloud/
├── main.py
├── voice_agent.py
├── twilio_handler.py
├── patient_service.py
├── database.py
├── models.py
├── schemas.py
├── test_api.py
├── requirements.txt
├── static/
│   ├── index.html
│   └── dashboard.html
└── .fastapicloud/
    └── cloud.json
```

## Summary

CareCloud provides the same patient-registration agent through three interfaces:

1. **Live phone call** through Twilio
2. **Browser voice interface**
3. **REST API**

All patient creation paths ultimately use the same validation and persistence layer, while the voice agent requires explicit confirmation before saving a registration.
