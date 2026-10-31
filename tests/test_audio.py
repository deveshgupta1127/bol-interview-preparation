import numpy as np

from app import audio

RATE = 16000


def tone(seconds, freq=150.0, amp=0.3):
    t = np.arange(int(seconds * RATE)) / RATE
    # harmonics make it more voice-like for the pitch tracker
    return (amp * (np.sin(2 * np.pi * freq * t) + 0.5 * np.sin(4 * np.pi * freq * t))).astype(np.float32)


def silence(seconds):
    return (np.random.default_rng(0).normal(0, 0.0005, int(seconds * RATE))).astype(np.float32)


def test_wav_round_trip():
    samples = tone(1.0)
    back, rate = audio.read_wav(audio.to_wav(samples, RATE))
    assert rate == RATE and len(back) == len(samples)
    assert np.abs(back - samples).max() < 1e-3


def test_rejects_non_wav():
    try:
        audio.read_wav(b"not a wav file")
    except audio.AudioError:
        return
    raise AssertionError("expected AudioError")


def test_pause_and_speech_time():
    signal = np.concatenate([silence(0.5), tone(1.5), silence(1.2), tone(1.5), silence(0.5)])
    f = audio.analyse(signal, RATE)
    assert f["long_pauses"] == 1
    assert 2.7 < f["speech_s"] < 3.3
    assert 1.0 < f["longest_pause_s"] < 1.4


def test_short_gaps_between_words_are_not_pauses():
    signal = np.concatenate([tone(0.6), silence(0.2), tone(0.6), silence(0.2), tone(0.6)])
    assert audio.analyse(signal, RATE)["long_pauses"] == 0


def test_flat_vs_varied_pitch():
    flat = audio.analyse(tone(3.0, 150), RATE)["pitch_variation_st"]
    t = np.arange(3 * RATE) / RATE
    freq = 120 + 60 * (1 + np.sin(2 * np.pi * 0.7 * t))  # 120-240 Hz
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    varied = audio.analyse((0.3 * (np.sin(phase) + 0.5 * np.sin(2 * phase))).astype(np.float32), RATE)
    assert flat is not None and flat < 0.5
    assert varied["pitch_variation_st"] > 2


def test_trailing_off():
    steady = tone(2.0)
    fading = steady.copy()
    n = len(fading)
    fading[int(n * 0.8):] *= 0.25  # last fifth about 12 dB quieter
    assert audio.analyse(steady, RATE)["trailing_off_db"] < 1
    assert audio.analyse(fading, RATE)["trailing_off_db"] > 6


def test_split_long_answer():
    signal = np.concatenate([tone(25), silence(1), tone(25), silence(1), tone(20)])
    chunks = audio.split_wav(signal, RATE, max_seconds=30)
    lengths = [len(audio.read_wav(c)[0]) for c in chunks]
    assert len(chunks) == 3
    assert max(lengths) <= 30 * RATE
    assert sum(lengths) == len(signal)


def speechlike(seconds):
    """Syllables of 0.14 s, each with its own pitch and tone colour, like running speech."""
    pitches = [110, 150, 125, 180, 135, 200, 118, 165, 142, 190]
    weights = [(1, .2, .6), (1, .8, .1), (1, .4, .4), (1, .1, .9), (1, .6, .2)]
    out, k = [], 0
    while sum(len(x) for x in out) < seconds * RATE:
        f, w = pitches[k % len(pitches)], weights[k % len(weights)]
        t = np.arange(int(0.14 * RATE)) / RATE
        syl = sum(a * np.sin(2 * np.pi * f * (h + 1) * t) for h, a in enumerate(w)) * np.hanning(len(t)) * 0.25
        out += [syl.astype(np.float32), silence(0.04)]
        k += 1
    return np.concatenate(out)


def test_umm_is_counted_from_audio():
    t = np.arange(int(0.6 * RATE)) / RATE
    umm = (0.25 * (np.sin(2 * np.pi * 130 * t) + 0.4 * np.sin(2 * np.pi * 260 * t))).astype(np.float32)
    signal = np.concatenate([silence(0.3), speechlike(2), umm, speechlike(2), silence(0.3)])
    count, seconds = audio.hesitations(signal, RATE)
    assert count == 1 and 0.5 < seconds < 0.7
    assert audio.analyse(signal, RATE)["hesitations"] == 1


def test_running_speech_has_no_umm():
    assert audio.hesitations(np.concatenate([silence(0.3), speechlike(5), silence(0.3)]), RATE)[0] == 0


def test_long_held_tone_is_not_umm():
    assert audio.hesitations(np.concatenate([silence(0.3), tone(3.0), silence(0.3)]), RATE)[0] == 0
