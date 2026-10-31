# Bol: interview coach

Upload a resume and a job description. Bol asks the questions an interviewer would ask *you*, listens to your
spoken answer, and coaches you, with the explanation in your own language.

| Step | Done by |
| - | - |
| Answer transcribed, Hindi words like "matlab" included (Prisma v3.0 auto-detect) | Gnani **Prisma** speech-to-text |
| "umm"/"uh" (Prisma drops them) | counted from the audio: steady-pitch sounds of 0.3-2 s (`app/audio.py`) |
| Pace, pauses, loudness, trailing off, pitch variation | numpy on the server (`app/audio.py`) |
| Fillers, Hindi/English mix, Indian-English phrasing | rules (`app/language.py`) |
| Questions from resume + JD, rubric score, rewrite, tips, follow-up, mother-tongue feedback | **Claude Haiku 4.5**, called only from the backend (`app/claude.py`) |
| Rewrite fact check (no invented numbers, tools or companies) | `app/factcheck.py`, with one repair call to Claude |
| Question read aloud, feedback spoken in Hindi/Tamil/Kannada | Gnani **Timbre** text-to-speech |
| Suggested answer spoken in your own voice | Gnani voice cloning (your own consented sample) |

Score = 0.5 content + 0.25 fluency + 0.25 delivery (typed answers: content and fluency only).

## Run it

```bash
cd bol
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env      # then fill in the keys
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8020
```

Open http://localhost:8020. In the Claude desktop app you can also start the `bol` preview from `.claude/launch.json`.

### Keys (`.env`)

| Variable | What for | Without it |
| - | - | - |
| `ANTHROPIC_API_KEY` | Claude coaching (from console.anthropic.com; set a monthly spend limit there) | template questions and sample coaching |
| `CLAUDE_MODEL` | defaults to `claude-haiku-4-5`; `claude-haiku-5-5` also works | |
| `VACHANA_API_KEY` | Gnani speech-to-text, voices and voice cloning (app.gnani.ai) | type answers; no audio |
| `STT_MODEL` / `STT_LANGUAGE` / `STT_FORMAT` | Prisma model and output style (default `prisma-v3.0`, `indic-auto`, `codemix`) | |
| `TTS_ENGLISH_VOICE` / `TTS_ENGLISH_LANGUAGE` | voice that reads questions and model answers (default `Poorvi` / `en-IN`) | |
| `TTS_VOICE_HI_IN`, `TTS_VOICE_TA_IN`, ... | override the feedback voice per language | |

Cost: every feedback screen shows the Claude tokens and dollars for that answer. One answer is roughly 4k input + 1k output tokens on Haiku 4.5 ($1 / $5 per million), about 1 US cent
(under ₹1); preparing a session costs about the same.

## Tests

```bash
.venv\Scripts\python -m pytest -q
```

The tests never call paid APIs: keys are blanked and the network calls are replaced.

## Privacy

- Audio is processed in memory and never stored. Resumes are stored only after phone numbers, emails, links and ID
  numbers are removed.
- Voice cloning needs a consent tick and only your own voice; "Delete my voice" removes it.
- For demos use synthetic resumes (see `samples/`), never real people's.

## Running on Azure (Phase 2)

Locally nothing changes: no sign-in, SQLite, the API serves `web/`. On Azure the same code runs with
environment variables set (all listed in `.env.example`):

| Part | Azure | In the code |
| - | - | - |
| `web/` | Static Web Apps (Free) | `web/config.js` gets the API URL and sign-in settings at deploy time |
| `app/` | Container Apps, from the `Dockerfile` | `SERVE_WEB=false`; CORS allows only the site |
| Database | PostgreSQL Flexible Server B1ms | `DATABASE_URL`; tables made and upgraded by Alembic at start-up |
| Sign-in | Entra External ID (email code, Google) | MSAL.js in the browser; `app/auth.py` checks every token |
| Cached speech | Blob Storage, `tts-cache`, 7-day lifecycle rule | `app/tts_cache.py`, managed identity |
| Logs and timings | Application Insights | OpenTelemetry; no transcripts, resumes or tokens in logs |

Many students at once:

- Every row belongs to the signed-in student; other students' sessions, answers and voice return 404.
- One answer at a time per student (a second one gets 429). A retried upload with the same `Idempotency-Key`
  returns the first result instead of being scored and paid for twice.
- Daily limits per student (30 answers, 5 sessions, 40 audio plays, 3 voice set-ups) and a monthly Claude
  budget for the whole app ($25). When the budget is used up, coaching switches to the sample coaching.
- `DELETE /api/me` (Settings → Delete my account) removes everything of that student.

Guardrails:

- **Prompt injection.** Student text (resume, job description, answer) sits inside tags Claude is told to treat as
  data. `app/safety.py` removes those tags from student text, so a typed `</answer>` can't end the section early.
  Short fields outside the tags (role, question) are flattened to one line. Claude has no tools and sees only that
  student's own data.
- **Checking what Claude writes.** Every field a student reads or hears is checked for abuse (English, Hinglish,
  Devanagari) and hijack phrases. A failed reply falls back to the sample coaching with a note. Links, emails and
  phone numbers are removed, and lengths are capped (spoken feedback at 1,200 characters, which also caps Timbre
  cost). The check is local: no cost, no delay. Other languages are checked for links and leaks, not for abuse.
- **Rate limits.** Per student: at most 10 expensive calls a minute (new sessions, answers, voice set-ups and
  new audio). Per address: 120 API calls a minute, counted before sign-in is checked; `/api/health` is never
  limited. Bodies over 10 MB are refused before they're read, and typed answers are capped at 3,000 characters.
  The per-minute counts live in each replica's memory (up to 3x the limit across 3 replicas); the daily limits
  are exact. `TRUSTED_PROXY_HOPS=1` on Container Apps, so a faked `X-Forwarded-For` can't dodge them.
- **Sign-out.** After 30 minutes without activity the browser signs the student out, at Microsoft too, and clears
  the tab's stored data. Expired tokens are refused by the API; the browser renews them silently or signs in again.
- **Browser security headers** in `web/staticwebapp.config.json`: a Content Security Policy that allows only
  this site, the MSAL script, Google Fonts and Entra sign-in. Pages can't be framed, and the microphone is allowed
  only for this site. The deploy step must replace `__API_ORIGIN__` with the API's address (for example
  `https://bol-api.<region>.azurecontainerapps.io`). Local runs send the same headers.

Build and run the image locally:

```bash
docker build -t bol-api .
docker run --rm -p 8000:8000 --env-file .env bol-api
```
