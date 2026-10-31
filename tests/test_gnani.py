"""Prisma request shape, with the network call replaced."""
import dataclasses
import json
from types import SimpleNamespace

from app import audio, config, gnani


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def test_prisma_v3_auto_detect_and_bias_retry(monkeypatch):
    monkeypatch.setattr(gnani, "settings", dataclasses.replace(config.settings, vachana_api_key="test-key"))
    sent = []

    def fake_post(url, headers, data, files, timeout):
        sent.append(data)
        if "bias_list" in data:
            return FakeResponse(400, {"success": False, "error": {"message": "bad bias"}})
        return FakeResponse(200, {"success": True, "transcript": "मतलब I built the backend"})

    monkeypatch.setattr(gnani.httpx, "post", fake_post)
    assert gnani.transcribe(b"wav", bias=["Flask"]) == "मतलब I built the backend"
    assert sent[0]["model"] == "prisma-v3.0" and sent[0]["language_code"] == "indic-auto"
    assert sent[0]["format"] == "codemix" and "bias_list" in sent[0]
    assert "bias_list" not in sent[1]  # retried without the optional bias list


# ---------- one request at a time (the key allows only one active request) ----------

def _rate_limited():
    return FakeResponse(429, {"error_code": "RATE_LIMITED", "message": "Only 1 active request is allowed at a time."})


def test_busy_key_is_retried(monkeypatch):
    monkeypatch.setattr(gnani, "settings", dataclasses.replace(config.settings, vachana_api_key="test-key"))
    monkeypatch.setattr(gnani, "BUSY_RETRIES", (0, 0))
    replies = [_rate_limited(), _rate_limited(), FakeResponse(200, {"success": True, "transcript": "ok"})]
    monkeypatch.setattr(gnani.httpx, "post", lambda *a, **k: replies.pop(0))
    assert gnani.transcribe(b"wav") == "ok"


def test_busy_key_gives_up_with_a_clear_error(monkeypatch):
    monkeypatch.setattr(gnani, "settings", dataclasses.replace(config.settings, vachana_api_key="test-key"))
    monkeypatch.setattr(gnani, "BUSY_RETRIES", (0, 0))
    monkeypatch.setattr(gnani.httpx, "post", lambda *a, **k: _rate_limited())
    try:
        gnani.transcribe(b"wav")
    except gnani.GnaniError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("expected GnaniError")


def test_long_answer_is_sent_in_order_one_piece_at_a_time(monkeypatch):
    import threading
    import time

    import numpy as np

    monkeypatch.setattr(gnani, "settings", dataclasses.replace(config.settings, vachana_api_key="test-key"))
    active, most, pieces = [0], [0], []
    lock = threading.Lock()

    def fake_post(url, headers, timeout, data, files):
        with lock:
            active[0] += 1
            most[0] = max(most[0], active[0])
        time.sleep(0.01)
        with lock:
            active[0] -= 1
        samples, rate = audio.read_wav(files["audio_file"][1])
        pieces.append(len(samples) / rate)
        return FakeResponse(200, {"success": True, "transcript": f"part{len(pieces)}"})

    monkeypatch.setattr(gnani.httpx, "post", fake_post)
    rate = 16000
    three_minutes = (0.1 * np.sin(np.arange(180 * rate) / rate * 2 * np.pi * 150)).astype(np.float32)
    text = gnani.transcribe_answer(three_minutes, rate)
    assert most[0] == 1  # never two requests at once
    assert all(seconds <= 28 for seconds in pieces)  # Prisma v3.0 refuses clips over 30 s
    assert round(sum(pieces)) == 180 and text == " ".join(f"part{i}" for i in range(1, len(pieces) + 1))


def test_other_threads_wait_for_their_turn(monkeypatch):
    """Two students at once on one server: their Gnani calls queue instead of colliding."""
    import threading
    import time

    monkeypatch.setattr(gnani, "settings", dataclasses.replace(config.settings, vachana_api_key="test-key"))
    active, most = [0], [0]
    lock = threading.Lock()

    def fake_post(*a, **k):
        with lock:
            active[0] += 1
            most[0] = max(most[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return SimpleNamespace(status_code=200, content=b"RIFF")

    monkeypatch.setattr(gnani.httpx, "post", fake_post)
    threads = [threading.Thread(target=gnani.synthesize, args=("Hi", "en-IN", "Poorvi")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most[0] == 1
