"""WAV handling and delivery features computed on the server with numpy (no audio is stored).

Features: speaking time, long pauses, loudness, end-of-sentence drop ("trailing off"), and pitch variation.
They are compared with the student's own earlier attempts in the UI, never against a fixed voice norm.
"""
import io
import wave

import numpy as np

FRAME_S, HOP_S = 0.025, 0.010
LONG_PAUSE_S = 0.7
MIN_SEGMENT_S = 1.0


class AudioError(ValueError):
    pass


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    """16-bit PCM WAV -> mono float32 samples in [-1, 1] and the sample rate."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2:
                raise AudioError("Recording must be 16-bit PCM WAV.")
            rate, channels = w.getframerate(), w.getnchannels()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioError("Please send a WAV recording.") from exc
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def to_wav(samples: np.ndarray, rate: int) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def split_wav(samples: np.ndarray, rate: int, max_seconds: float = 30.0) -> list[bytes]:
    """Prisma v3.0 takes clips up to 30 s, so long answers go in chunks (cut at the quietest point)."""
    step = int(max_seconds * rate)
    chunks, start = [], 0
    while start < len(samples):
        end = min(len(samples), start + step)
        if end < len(samples):  # look back up to 3 s for a quiet place to cut
            window = samples[max(start, end - 3 * rate):end]
            frame = int(0.05 * rate)
            if len(window) > frame:
                energy = np.convolve(window ** 2, np.ones(frame), "valid")
                end = max(start, end - 3 * rate) + int(np.argmin(energy))
                end = max(end, start + rate)  # never a chunk shorter than 1 s
        chunks.append(to_wav(samples[start:end], rate))
        start = end
    return chunks


def _frames(samples: np.ndarray, rate: int, size: int | None = None) -> np.ndarray:
    size, hop = size or int(FRAME_S * rate), int(HOP_S * rate)
    if len(samples) < size:
        return np.zeros((0, size), dtype=np.float32)
    count = 1 + (len(samples) - size) // hop
    idx = np.arange(size)[None, :] + hop * np.arange(count)[:, None]
    return samples[idx]


def _segments(voiced: np.ndarray) -> list[tuple[int, int]]:
    """Runs of voiced frames, bridging gaps shorter than 0.3 s (normal between words)."""
    bridge = int(0.3 / HOP_S)
    segs, start, gap = [], None, 0
    for i, v in enumerate(voiced):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > bridge:
                segs.append((start, i - gap + 1))
                start, gap = None, 0
    if start is not None:
        segs.append((start, len(voiced) - gap))
    return segs


def _pitch(frame: np.ndarray, rate: int) -> float | None:
    """Autocorrelation pitch estimate in 75-400 Hz; None when the frame isn't clearly periodic."""
    frame = frame - frame.mean()
    ac = np.correlate(frame, frame, "full")[len(frame) - 1:]
    if ac[0] <= 0:
        return None
    lo, hi = int(rate / 400), min(len(ac) - 1, int(rate / 75))
    if hi <= lo:
        return None
    lag = lo + int(np.argmax(ac[lo:hi]))
    return rate / lag if ac[lag] / ac[0] > 0.3 else None


HES_MIN_S, HES_MAX_S = 0.3, 2.0  # how long an "umm"/"uh" lasts; held tones longer than this aren't speech
_BANDS = 24


def _pitch_and_shape(frames: np.ndarray, rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Per frame: pitch in Hz (0 = not clearly voiced) by FFT autocorrelation, and the unit-length log spectrum
    in 24 bands up to 4 kHz (its "tone colour"). Done in chunks to keep memory small for 3-minute answers."""
    n = frames.shape[1]
    nfft = 1 << (2 * n - 1).bit_length()
    lo, hi = int(rate / 400), min(n - 1, int(rate / 75))
    edges = np.linspace(1, int(4000 / rate * nfft), _BANDS + 1).astype(int)
    window = np.hanning(n)
    pitch, shape = np.zeros(len(frames)), np.zeros((len(frames), _BANDS), dtype=np.float32)
    for start in range(0, len(frames), 1000):
        x = frames[start:start + 1000]
        x = x - x.mean(axis=1, keepdims=True)
        ac = np.fft.irfft(np.abs(np.fft.rfft(x, nfft)) ** 2, nfft)[:, :n]
        lags = lo + np.argmax(ac[:, lo:hi], axis=1)
        strength = ac[np.arange(len(x)), lags] / np.maximum(ac[:, 0], 1e-12)
        pitch[start:start + len(x)] = np.where(strength > 0.5, rate / lags, 0.0)
        mag = np.abs(np.fft.rfft(x * window, nfft))
        bands = np.log1p(np.add.reduceat(mag, edges[:-1], axis=1)[:, :_BANDS])
        bands -= bands.mean(axis=1, keepdims=True)
        shape[start:start + len(x)] = bands / np.maximum(np.linalg.norm(bands, axis=1, keepdims=True), 1e-9)
    return pitch, shape


def hesitations(samples: np.ndarray, rate: int) -> tuple[int, float]:
    """Count filled pauses ("umm", "uh", a long-drawn "sooo") from the audio itself.

    Prisma's transcript drops them, so they are found acoustically: a voiced stretch of 0.3-2 s whose pitch and
    tone colour barely move. Normal speech changes both every syllable (about every 0.1-0.25 s).
    Returns (count, total seconds). An estimate, shown to the student as such.
    """
    size, hop = int(0.04 * rate), int(HOP_S * rate)
    frames = _frames(samples, rate, size)
    if len(frames) < 3:
        return 0, 0.0
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    floor, peak = np.percentile(db, 10), np.percentile(db, 95)
    voiced = db > max(min(floor + 12, peak - 20), -50)
    pitch, shape = _pitch_and_shape(frames, rate)
    steady = voiced & (pitch > 0)
    semis = 12 * np.log2(np.maximum(pitch, 1) / 100)
    link = (steady[1:] & steady[:-1] & (np.abs(np.diff(semis)) < 0.5)
            & ((shape[1:] * shape[:-1]).sum(axis=1) > 0.9))
    count, total = 0, 0.0
    i = 0
    while i < len(link):
        if not link[i]:
            i += 1
            continue
        j = i
        while j < len(link) and link[j]:
            j += 1
        seconds = (j - i + 1) * hop / rate
        run = semis[i:j + 1]
        if HES_MIN_S <= seconds <= HES_MAX_S and run.max() - run.min() < 2:
            count += 1
            total += seconds
        i = j
    return count, round(total, 2)


def analyse(samples: np.ndarray, rate: int) -> dict:
    duration = len(samples) / rate
    frames = _frames(samples, rate)
    if len(frames) == 0:
        return {"duration_s": round(duration, 2), "speech_s": 0.0}
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    floor, peak = np.percentile(db, 10), np.percentile(db, 95)
    # 12 dB above the room's noise floor, but never so high that steady speech with no silence counts as silence
    voiced = db > max(min(floor + 12, peak - 20), -50)
    segs = [s for s in _segments(voiced) if (s[1] - s[0]) * HOP_S >= 0.15]
    speech_s = sum(b - a for a, b in segs) * HOP_S

    pauses = [(segs[i + 1][0] - segs[i][1]) * HOP_S for i in range(len(segs) - 1)]
    long_pauses = [p for p in pauses if p >= LONG_PAUSE_S]

    drops = []
    for a, b in segs:
        if (b - a) * HOP_S >= MIN_SEGMENT_S:
            cut = a + int((b - a) * 0.8)
            drops.append(float(db[a:cut].mean() - db[cut:b].mean()))

    pitches = []
    step = max(1, int(0.05 / HOP_S))  # every 50 ms of voiced audio
    pitch_frame = int(0.04 * rate)
    hop = int(HOP_S * rate)
    for a, b in segs:
        for i in range(a, b, step):
            chunk = samples[i * hop:i * hop + pitch_frame]
            if len(chunk) == pitch_frame:
                p = _pitch(chunk, rate)
                if p:
                    pitches.append(p)
    pitch_var = None
    if len(pitches) >= 10:
        semis = 12 * np.log2(np.array(pitches) / np.median(pitches))
        pitch_var = round(float(np.std(semis)), 2)
    hes_count, hes_seconds = hesitations(samples, rate)

    return {
        "hesitations": hes_count,
        "hesitation_s": hes_seconds,
        "duration_s": round(duration, 2),
        "speech_s": round(speech_s, 2),
        "long_pauses": len(long_pauses),
        "longest_pause_s": round(max(pauses), 2) if pauses else 0.0,
        "loudness_db": round(float(db[voiced].mean()), 1) if voiced.any() else None,
        "trailing_off_db": round(float(np.median(drops)), 1) if drops else None,
        "pitch_variation_st": pitch_var,
    }
