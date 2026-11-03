"""Live feedback: the coach starts talking while Claude is still writing.

Claude writes the mother-tongue feedback first (claude.EvalReply puts it first). Each finished sentence goes to
Gnani's streaming voice straight away, and the audio goes to the browser while Claude finishes the scores, tips and
better answer. Sentences that arrive while one is being spoken are joined and sent together, so a reply of five
sentences is usually two or three Gnani requests (Gnani allows one request at a time per key).

The browser reads newline-delimited JSON events:
  {"type": "transcript", "text": ...}       what the student said
  {"type": "voice", "rate": 22050}          speech follows: raw 16-bit mono PCM at that rate
  {"type": "audio", "data": <base64>}       one piece of it
  {"type": "result", "data": {...}}         the same reply /api/score returns as plain JSON
  {"type": "voice_end", "ok": true,         ok: what was spoken is exactly result.native_feedback, so the browser
   "stale": false}                          can keep the recording for "Listen" instead of asking Gnani again.
                                            stale: what was spoken isn't the start of the final feedback (it was
                                            replaced, e.g. by the stand-in), so the browser should stop playing it
  {"type": "error", "status": 502, "detail": "..."}

Every sentence is checked before it is spoken (safety words, and the language's own script) because the full
safety pass on Claude's reply only runs once the reply is complete. If a check fails, the voice stops and the
student reads the feedback instead.
"""
import base64
import json
import logging
import queue
import threading
from typing import Callable, Iterator

from fastapi import HTTPException

from . import gnani, limits, rules, safety

log = logging.getLogger("bol.live")
MAX_SPOKEN_CHARS = 1300  # feedback is capped at 1,200 characters; this also caps the Gnani cost


def _line(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


def _norm(text: str) -> str:
    return " ".join(text.split())


def replay(reply: dict) -> Iterator[bytes]:
    """A retried upload that was already scored: the same result again, without the voice."""
    yield _line({"type": "transcript", "text": reply.get("transcript", "")})
    yield _line({"type": "result", "data": reply})


def run(transcript: str, coach: Callable[[Callable[[str], None] | None], dict], language: str, voice: str | None,
        on_result: Callable[[dict], None]) -> Iterator[bytes]:
    """coach(on_feedback) scores the answer and returns the reply; on_feedback gets the feedback as it's written.
    voice: the Timbre voice for the feedback language, or None to send no audio."""
    out: queue.Queue = queue.Queue()
    sentences: queue.Queue = queue.Queue()
    state = {"speak": voice is not None, "cancelled": False, "why": ""}  # why: the reason the voice stopped early
    spoken: list[str] = []

    def silence(why: str) -> None:
        if state["speak"]:
            state["speak"], state["why"] = False, why

    def on_feedback(text: str) -> None:
        if state["speak"]:
            sentences.put(text)

    def coach_worker() -> None:
        try:
            out.put(("result", coach(on_feedback if voice else None)))
        except HTTPException as exc:
            out.put(("error", exc.status_code, str(exc.detail)))
        except Exception:
            log.exception("live scoring failed")
            out.put(("error", 500, "Something went wrong while scoring. Please try again."))
        finally:
            sentences.put(None)  # no more feedback text is coming

    def speaker_worker() -> None:
        heard = ""
        try:
            finished = False
            while not finished:
                batch = [sentences.get()]
                while True:  # take everything else that is already waiting
                    try:
                        batch.append(sentences.get_nowait())
                    except queue.Empty:
                        break
                finished = None in batch
                text = " ".join(b for b in batch if b).strip()
                if not text or not state["speak"] or state["cancelled"]:
                    continue
                text = safety.clean(text, MAX_SPOKEN_CHARS)  # the same cleaning the shown feedback gets
                so_far = f"{heard} {text}".strip()
                problem = ("safety" if safety.problem(text) else "script" if not rules.in_own_script(so_far, language)
                           else "length" if len(so_far) > MAX_SPOKEN_CHARS else "")
                if problem:
                    silence(problem)  # the student reads it instead
                    continue
                speech = gnani.stream_speech(text, language, voice)
                try:
                    for chunk in speech:
                        if state["cancelled"]:
                            break
                        out.put(("audio", chunk))
                finally:
                    speech.close()
                limits.budget.add(timbre_chars=len(text))
                spoken.append(text)
                heard = so_far
        except gnani.GnaniError as exc:
            log.warning("live feedback voice stopped: %s", exc)
            silence("gnani")
        except Exception:
            log.exception("live feedback voice failed")
            silence("error")
        finally:
            out.put(("speaker_done",))

    threading.Thread(target=coach_worker, daemon=True).start()
    if voice:
        threading.Thread(target=speaker_worker, daemon=True).start()

    try:
        yield _line({"type": "transcript", "text": transcript})
        result, speaker_done, voice_started = None, voice is None, False
        while result is None or not speaker_done:
            item = out.get()
            if item[0] == "audio":
                if not voice_started:
                    voice_started = True
                    yield _line({"type": "voice", "rate": gnani.PCM_RATE})
                yield _line({"type": "audio", "data": base64.b64encode(item[1]).decode()})
            elif item[0] == "result":
                result = item[1]
                on_result(result)
                if not _norm(result["native_feedback"]).startswith(_norm(" ".join(spoken))):
                    silence("changed")  # the feedback changed (e.g. a fallback): stop reading the old one
                yield _line({"type": "result", "data": result})
            elif item[0] == "error":
                yield _line({"type": "error", "status": item[1], "detail": item[2]})
                return
            else:
                speaker_done = True
        said, final = _norm(" ".join(spoken)), _norm(result["native_feedback"])
        ok = voice_started and state["speak"] and said == final
        if voice and not ok:  # lengths and a reason only; never the text
            log.info("live voice incomplete: spoke %d of %d chars (%s)", len(said), len(final),
                     state["why"] or ("no audio" if not voice_started else "text differs"))
        yield _line({"type": "voice_end", "ok": ok, "stale": not final.startswith(said)})
    finally:
        state["cancelled"] = True  # the browser left: stop paying for speech nobody hears
        state["speak"] = False
