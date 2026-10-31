"""Turn measurements into 0-100 scores and plain-language observations.

overall = 0.5 content + 0.25 fluency + 0.25 delivery (re-weighted over the parts available,
e.g. a typed answer has no delivery score).
"""

WEIGHTS = {"content": 0.5, "fluency": 0.25, "delivery": 0.25}
PACE = (120, 160)  # words per minute that sound calm and clear in interviews


def _cap(value: float, limit: float) -> float:
    return min(max(value, 0.0), limit)


def delivery(acoustic: dict, lang: dict) -> tuple[int | None, list[str]]:
    if not acoustic or not acoustic.get("speech_s"):
        return None, []
    score, notes = 100.0, []
    minutes = max(acoustic.get("duration_s", acoustic["speech_s"]) / 60, 0.1)

    wpm = lang.get("wpm")
    if wpm:
        if wpm < PACE[0]:
            score -= _cap((PACE[0] - wpm) * 0.5, 30)
            notes.append(f"Pace {wpm} words/min is slow; aim for {PACE[0]}-{PACE[1]}.")
        elif wpm > PACE[1]:
            score -= _cap((wpm - PACE[1]) * 0.5, 30)
            notes.append(f"Pace {wpm} words/min is fast; slow down to {PACE[0]}-{PACE[1]}.")
        else:
            notes.append(f"Good pace: {wpm} words/min.")

    pauses_per_min = acoustic.get("long_pauses", 0) / minutes
    if pauses_per_min > 4:
        score -= _cap((pauses_per_min - 4) * 4, 20)
        notes.append(f"{acoustic['long_pauses']} long pauses (over 0.7 s). Pause between points, not mid-sentence.")

    trail = acoustic.get("trailing_off_db")
    if trail is not None and trail > 4:
        score -= _cap((trail - 4) * 3, 15)
        notes.append("Your voice drops at the end of sentences. Finish each sentence as strongly as you start it.")

    pitch = acoustic.get("pitch_variation_st")
    if pitch is not None and pitch < 1.5:
        score -= _cap((1.5 - pitch) * 10, 15)
        notes.append("Your tone is quite flat. Stress the key words (numbers, results, tools).")

    loud = acoustic.get("loudness_db")
    if loud is not None and loud < -35:
        score -= _cap((-35 - loud) * 2, 15)
        notes.append("You sound quiet. Sit closer to the mic or speak up a little.")
    return round(_cap(score, 100)), notes


def fluency(lang: dict, spoken: bool) -> tuple[int, list[str]]:
    score, notes = 100.0, []
    per_min = lang.get("fillers_per_min")
    if spoken and per_min is not None and per_min > 2:
        score -= _cap((per_min - 2) * 6, 40)
    if lang.get("filler_count"):
        top = sorted(lang["fillers"].items(), key=lambda kv: -kv[1])[:3]
        notes.append("Fillers: " + ", ".join(f"\"{w}\" x{n}" for w, n in top) + ". Pause silently instead.")
    if lang.get("hesitations"):
        n = lang["hesitations"]
        notes.append(f"About {n} \"umm\"/\"uh\" sound{'s' if n > 1 else ''} heard in your voice. "
                     "Close your mouth and pause instead.")

    if lang.get("repeats"):
        score -= _cap(lang["repeats"] * 3, 15)
        notes.append(f"{lang['repeats']} repeated words (like \"I I\"). Slow down slightly before key points.")

    share = lang.get("english_share")
    if share is not None and share < 90:
        score -= _cap((90 - share) * 0.5, 30)
        notes.append(f"{share:.0f}% of your words were English. Practise the English words for the Hindi ones you used.")

    hits = lang.get("indian_english") or []
    if hits:
        score -= _cap(len(hits) * 4, 12)
    return round(_cap(score, 100)), notes


def content(rubric: dict | None) -> int | None:
    if not rubric:
        return None
    total = sum(int(rubric[k]["score"]) for k in ("answered", "structure", "specifics", "jd_fit"))
    return round(total / 12 * 100)


def overall(parts: dict) -> int | None:
    available = {k: v for k, v in parts.items() if v is not None and k in WEIGHTS}
    if not available:
        return None
    weight = sum(WEIGHTS[k] for k in available)
    return round(sum(WEIGHTS[k] * v for k, v in available.items()) / weight)
