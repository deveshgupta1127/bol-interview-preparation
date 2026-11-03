"""Gnani speech APIs: Prisma (speech-to-text), Timbre (text-to-speech) and voice cloning.

One VACHANA_API_KEY covers all of them. Docs: https://docs.gnani.ai/api/introduction/introduction

The key allows ONE active request at a time, across all three services (tested 30 Oct: a second parallel request
gets 429 "Only 1 active request is allowed at a time"). So every call here waits for its turn in this process, and
retries with a short back-off when another process using the same key (another replica) is busy.
"""
import base64
import json
import logging
import os
import re
import threading
import time
from typing import Iterator

import httpx
import numpy as np

from . import audio
from .config import settings

# Timbre voices per feedback language (from Gnani's voice list). Override with TTS_VOICE_<LANG>, e.g. TTS_VOICE_HI_IN.
FEEDBACK_VOICES = {"hi-IN": "Nalini", "ta-IN": "Asmita", "kn-IN": "Saanvi"}
# Voice that reads interview questions and model answers in English when the student hasn't cloned their voice.
ENGLISH_VOICE = os.getenv("TTS_ENGLISH_VOICE", "Poorvi")
ENGLISH_LANGUAGE = os.getenv("TTS_ENGLISH_LANGUAGE", "en-IN")
WAV_CONFIG = {"sample_rate": 22050, "num_channels": 1, "sample_width": 2, "encoding": "linear_pcm", "container": "wav"}
# Streamed speech: the same audio with no WAV header, so each chunk can be played as it arrives.
PCM_RATE = WAV_CONFIG["sample_rate"]
PCM_CONFIG = {**WAV_CONFIG, "container": "raw"}
log = logging.getLogger("bol.gnani")


class GnaniError(RuntimeError):
    pass


_turn = threading.Lock()  # one Gnani request at a time from this process
TURN_WAIT_S = 120  # give up waiting for a turn after this (an answer turn has 240 s in total)
BUSY_RETRIES = (0.5, 1, 2, 4, 8)  # back-off after a 429 from another process: ~15 s in all


def _send(what: str, url: str, timeout: float, **kwargs) -> httpx.Response:
    """POST to Gnani when it's our turn; on 429 (key busy elsewhere) wait and try again."""
    for pause in (*BUSY_RETRIES, None):
        if not _turn.acquire(timeout=TURN_WAIT_S):
            raise GnaniError(f"{what} is busy with other students. Try again in a minute.")
        try:
            response = httpx.post(f"{settings.vachana_base_url}{url}", headers=_headers(), timeout=timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise GnaniError(f"{what} unreachable: {exc}") from exc
        finally:
            _turn.release()
        if response.status_code != 429 or pause is None:
            return response
        time.sleep(pause)  # without holding the turn, so this process's other calls aren't blocked
    raise AssertionError("unreachable")


def enabled() -> bool:
    return bool(settings.vachana_api_key)


def voice_for(language: str) -> str | None:
    override = os.getenv("TTS_VOICE_" + language.replace("-", "_").upper())
    return override or FEEDBACK_VOICES.get(language)


def _headers() -> dict:
    if not settings.vachana_api_key:
        raise GnaniError("VACHANA_API_KEY is not set")
    return {"X-API-Key-ID": settings.vachana_api_key}


def _raise_for(response: httpx.Response, what: str) -> None:
    if response.status_code < 400:
        return
    try:
        detail = response.json()
        detail = detail.get("error", {}).get("message") or detail.get("detail") or detail
    except ValueError:
        detail = response.text[:200]
    raise GnaniError(f"{what} failed ({response.status_code}): {detail}")


def bias_words(terms: list[str]) -> list[str]:
    """Prisma's bias list takes up to 100 single words made of letters only (e.g. FastAPI, Kubernetes)."""
    words: list[str] = []
    for term in terms:
        for word in re.split(r"[\s/,.+\-()]+", term):
            if re.fullmatch(r"[A-Za-z]{2,30}", word) and word.lower() not in {w.lower() for w in words}:
                words.append(word)
    return words[:100]


# Prisma v3.0 with indic-auto hears Hindi words like "matlab" inside an English answer (v2.5 takes one language).
# "codemix" keeps English words in Latin and Hindi in Devanagari; "verbatim" would write the English in Devanagari
# too, which breaks the English-share and Indian-English checks. Neither keeps "umm"/"uh" (counted from audio).
STT_MODEL = os.getenv("STT_MODEL", "prisma-v3.0")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "indic-auto")
STT_FORMAT = os.getenv("STT_FORMAT", "codemix")


def _post_stt(wav: bytes, data: dict) -> httpx.Response:
    return _send("Prisma STT", "/stt/v3", 60, data=data, files={"audio_file": ("answer.wav", wav, "audio/wav")})


def transcribe(wav: bytes, language: str | None = None, bias: list[str] | None = None) -> str:
    """One clip (<= 30 s on Prisma v3.0) -> transcript."""
    data = {"model": STT_MODEL, "language_code": language or STT_LANGUAGE, "format": STT_FORMAT}
    response = _post_stt(wav, {**data, "bias_list": json.dumps(bias[:100]), "bias_score": "1.0"} if bias else data)
    if bias and response.status_code == 400:
        response = _post_stt(wav, data)  # the bias list is optional; don't fail the answer over it
    _raise_for(response, "Prisma STT")
    body = response.json()
    if not body.get("success", True):
        raise GnaniError(f"Prisma STT error: {body}")
    return (body.get("transcript") or "").strip()


def transcribe_answer(samples: np.ndarray, rate: int, language: str | None = None,
                      bias: list[str] | None = None) -> str:
    """Whole answer (up to 3 min) -> transcript. Prisma v3.0 takes up to 30 s per request (400 "exceeds maximum
    allowed duration of 30 seconds") and one request at a time, so the answer goes in pieces of up to 28 s, one
    after another (a 3-minute answer is 7 requests)."""
    parts = [transcribe(chunk, language, bias) for chunk in audio.split_wav(samples, rate, max_seconds=28)]
    return " ".join(p for p in parts if p)


def synthesize(text: str, language: str, voice: str, speed: float = 0.95) -> bytes:
    payload = {"text": text, "voice": voice, "model": "timbre-v2.5", "language": language,
               "speed": max(0.85, min(1.15, speed)), "audio_config": WAV_CONFIG}
    response = _send("Timbre TTS", "/api/v1/tts/inference", 60, json=payload)
    _raise_for(response, "Timbre TTS")
    return response.content


def clone_embedding(wav: bytes) -> dict:
    """5-30 s of the student's own voice -> {embedding, shape, dtype}. Generated once and reused."""
    response = _send("Voice clone", "/api/v1/tts/voice-clone/embeddings", 90,
                     files={"audio_file": ("sample.wav", wav, "audio/wav")})
    _raise_for(response, "Voice clone")
    body = response.json()
    # The docs show both {"data": {"voice_clone_embedding": {...}}} and the bare object.
    found = (body.get("data") or {}).get("voice_clone_embedding") or body
    if not isinstance(found, dict) or not found.get("embedding"):
        raise GnaniError("Voice clone returned no embedding")
    return {"embedding": found["embedding"], "shape": found.get("shape"), "dtype": found.get("dtype")}


def synthesize_clone(text: str, embedding: dict) -> bytes:
    """Speak `text` in the student's cloned voice (same endpoint as TTS; the embedding switches the model)."""
    payload = {"text": text, "model": "vachana-vc-v1", "speaker_embedding": embedding, "audio_config": WAV_CONFIG}
    response = _send("Voice clone TTS", "/api/v1/tts/inference", 90, json=payload)
    _raise_for(response, "Voice clone TTS")
    return response.content


# ---------- streamed speech (Server-Sent Events: https://docs.gnani.ai/api/TTS/tts-sse) ----------

class Speech:
    """Raw 16-bit mono PCM at PCM_RATE, chunk by chunk, while Gnani is still speaking. Holds this process's Gnani
    turn until the stream ends or close() is called (iterating to the end closes it)."""

    def __init__(self, response: httpx.Response, client: httpx.Client, what: str) -> None:
        self._response, self._client, self.what, self._open = response, client, what, True

    def __iter__(self) -> Iterator[bytes]:
        try:
            event = None
            for line in self._response.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[5:])
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if event == "error" or data.get("type") == "error" or data.get("success") is False:
                    raise GnaniError(f"{self.what} failed: {data.get('message') or data}")
                audio = data.get("audio") or (data.get("data") or {}).get("audio")
                if audio:
                    yield base64.b64decode(audio)
        except httpx.HTTPError as exc:
            raise GnaniError(f"{self.what} stream dropped: {exc}") from exc
        finally:
            self.close()

    def close(self) -> None:
        if self._open:
            self._open = False
            self._response.close()
            self._client.close()
            _turn.release()

    def __del__(self) -> None:  # last resort if a stream is dropped unread: give the turn back
        self.close()


def _open_stream(what: str, payload: dict, timeout: float = 60) -> Speech:
    """Opens a speech stream when it's our turn; on 429 (key busy elsewhere) waits and tries again. Errors that
    Gnani reports before any audio raise GnaniError here."""
    headers = {**_headers(), "Accept": "text/event-stream"}
    for pause in (*BUSY_RETRIES, None):
        if not _turn.acquire(timeout=TURN_WAIT_S):
            raise GnaniError(f"{what} is busy with other students. Try again in a minute.")
        client = httpx.Client(timeout=timeout)
        try:
            request = client.build_request("POST", f"{settings.vachana_base_url}/api/v1/tts/sse", json=payload,
                                           headers=headers)
            response = client.send(request, stream=True)
        except httpx.HTTPError as exc:
            client.close()
            _turn.release()
            raise GnaniError(f"{what} unreachable: {exc}") from exc
        if response.status_code < 400:
            return Speech(response, client, what)
        response.read()
        response.close()
        client.close()
        _turn.release()
        if response.status_code != 429 or pause is None:
            _raise_for(response, what)
        time.sleep(pause)
    raise AssertionError("unreachable")


def stream_speech(text: str, language: str, voice: str, speed: float = 0.95) -> Speech:
    """Timbre voice, streamed."""
    return _open_stream("Timbre TTS", {"text": text, "voice": voice, "model": "timbre-v2.5", "language": language,
                                       "speed": max(0.85, min(1.15, speed)), "audio_config": PCM_CONFIG})


def stream_clone(text: str, embedding: dict) -> Speech:
    """The student's cloned voice, streamed."""
    return _open_stream("Voice clone TTS", {"text": text, "model": "vachana-vc-v1", "speaker_embedding": embedding,
                                            "audio_config": PCM_CONFIG}, timeout=90)
