# Bol: interview coach

Upload a resume and a job description. Bol asks the questions an interviewer would ask *you*, listens to your
spoken answer, and coaches you, with the explanation in your own language.

| Step | Done by |
| - | - |
| Answer transcribed, Hindi words like "matlab" included (Prisma v3.0 auto-detect) | Gnani **Prisma** speech-to-text |
| "umm"/"uh" (Prisma drops them) | counted from the audio: steady-pitch sounds of 0.3-2 s (`app/audio.py`) |
| Pace, pauses, loudness, trailing off, pitch variation | numpy on the server (`app/audio.py`) |
| Fillers, Hindi/English mix, Indian-English phrasing | rules (`app/language.py`) |
| Questions from resume + JD, rubric score, rewrite, tips, follow-up, mother-tongue feedback | **Claude Haiku 5.5**, called only from the backend (`app/claude.py`) |
| Rewrite fact check (no invented numbers, tools or companies) | `app/factcheck.py`, with one repair call to Claude |
| Question read aloud, feedback spoken in Hindi/Tamil/Kannada | Gnani **Timbre** text-to-speech, streamed |
| Feedback spoken while it's still being written | `app/live.py`: Claude streams the feedback first; each sentence goes to Timbre's streaming voice at once, so the coach starts talking about 3 s after you finish (the full score lands at about 7 s) |
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
| `CLAUDE_MODEL` | defaults to `claude-haiku-5-5`; `claude-haiku-4-5` also works | |
| `CLAUDE_EFFORT` / `CLAUDE_THINKING` | how much Haiku 5.5 thinks first (default `low` / on); `CLAUDE_THINKING=off` is ~0.7 s faster, slightly less consistent | |
| `VACHANA_API_KEY` | Gnani speech-to-text, voices and voice cloning (app.gnani.ai) | type answers; no audio |
| `STT_MODEL` / `STT_LANGUAGE` / `STT_FORMAT` | Prisma model and output style (default `prisma-v3.0`, `indic-auto`, `codemix`) | |
| `TTS_ENGLISH_VOICE` / `TTS_ENGLISH_LANGUAGE` | voice that reads questions and model answers (default `Poorvi` / `en-IN`) | |
| `TTS_VOICE_HI_IN`, `TTS_VOICE_TA_IN`, ... | override the feedback voice per language | |

Cost: every feedback screen shows the Claude tokens and dollars for that answer. On Haiku 5.5 ($0.10 / $0.50 per
million tokens) one answer is about $0.001 (3k input + 1-1.5k output, including the occasional repair call), a tenth
of a rupee; preparing a session costs about the same. Haiku 5.5 doesn't accept `temperature: 0`, so the same answer
can now differ by a point on one rubric line between runs (specifics and the speaking scores are rules, so they
don't move).

## Tests

```bash
.venv\Scripts\python -m pytest -q
```

The tests never call paid APIs: keys are blanked and the network calls are replaced.

## Privacy: Bol stores nothing

- No sign-in and no account. Your sessions, answers, scores and cloned voice live only in your browser's storage
  (`web/store.js`). Settings → "Download a backup" / "Restore a backup" moves them to another device;
  "Start fresh" deletes them, and so does clearing the browser's site data.
- The server keeps nothing: no database, no files. Each request carries what it needs (for example the resume
  summary along with an answer); the server analyses it in memory and forgets it. Recordings are never saved.
- Phone numbers, emails, links and ID numbers are removed from resumes before analysis.
- Voice cloning needs a consent tick and only your own voice; the voice data goes back to your browser only.
- For demos use synthetic resumes (see `samples/`), never real people's.

## Running on Azure

Locally nothing changes: the API serves `web/` too. On Azure (set up by `infra/provision.ps1`):

| Part | Azure | In the code |
| - | - | - |
| `web/` | Static Web Apps (Free, East Asia; served from the global edge) | `web/config.js` gets the API URL at deploy time |
| `app/` | Container Apps in UAE North, from the `Dockerfile`, 1 replica | `SERVE_WEB=false`; CORS allows only the site |
| Image | GitHub Container Registry (`ghcr.io`, free for a public repository) | built by GitHub Actions |
| API keys | Container app secrets | read as environment variables |
| Spend counters | Azure Table in a small storage account (numbers only; identity access, no keys) | `app/counters.py` |
| Logs and timings | Application Insights | OpenTelemetry; no transcripts, resumes or voice data in logs |

Many students at once, with nothing stored:

- The server is stateless; the browser sends the question and session details with each answer.
- Texts Bol may read aloud carry an HMAC signature, so `/api/speak` can't be used as free text-to-speech.
- Limits per address: 120 requests and 20 expensive calls a minute (in memory), and per day 10 sessions,
  60 answers, 120 audio plays and 5 voice set-ups (counters table, so they survive restarts). A monthly Claude
  budget for the whole app, also in the table. Addresses are stored only as one-way hashes. Behind all of it,
  the hard caps at the providers: the Claude workspace spend limit and the Gnani credits.
- Gnani allows one request at a time per key: Bol queues its Gnani calls and retries, and runs one replica.
- A retried upload with the same `Idempotency-Key` gets the first result instead of being scored twice.

Build and run the image locally:

```bash
docker build -t bol-api .
docker run --rm -p 8000:8000 --env-file .env bol-api
```
