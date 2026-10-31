from app import factcheck, language, pii, scoring


def test_english_and_hindi_fillers():
    text = "Um so basically I built the app, you know, and uh मतलब हमने उसको deploy किया।"
    found = language.fillers(text)
    assert found["um"] == 1 and found["uh"] == 1
    assert found["basically"] == 1 and found["you know"] == 1
    assert found["मतलब"] == 1


def test_like_only_counted_as_filler():
    assert "like" not in language.fillers("I like Python and I would like to learn Go.")
    assert language.fillers("It was, like, really hard")["like"] == 1


def test_fillers_are_whole_words():
    assert language.fillers("The umbrella and humble hummus") == {}


def test_repeats_and_english_share():
    assert language.repeats("I I worked on the the backend") == 2
    assert language.english_share("I worked on backend") == 100.0
    assert language.english_share("मैंने backend पर काम किया") == 20.0


def test_indian_english_phrases():
    hits = language.indian_english("Myself Ananya. I am having 2 years of coding practice since 2 years. "
                                   "I will revert back and we discussed about it.")
    said = {h["said"].lower() for h in hits}
    assert {"myself", "i am having", "since 2 years", "revert back", "discussed about"} <= said
    since = next(h for h in hits if h["said"].startswith("since"))
    assert since["better"] == "for 2 years"


def test_language_rates():
    result = language.analyse("um I built it um and deployed it", seconds=30)
    assert result["filler_count"] == 2
    assert result["fillers_per_min"] == 4.0
    assert result["wpm"] == 16


def test_pii_redaction():
    text = ("Ananya, ananya@example.com, +91 98765 43210, 9876543210, linkedin.com/in/ananya, "
            "https://github.com/ananya, 1234 5678 9012, ABCDE1234F, CGPA 7.8, 2023-2027")
    out = pii.redact(text)
    for secret in ["ananya@example.com", "98765", "9876543210", "linkedin.com", "github.com", "1234 5678", "ABCDE1234F"]:
        assert secret not in out
    assert "CGPA 7.8" in out and "2023-2027" in out


def test_factcheck_flags_invented_facts():
    resume = "Built a canteen app with Flask and MySQL used by about 300 students."
    answer = "um I made the canteen app in Flask"
    ok = "I built a canteen pre-order app with Flask and MySQL, used by about 300 students."
    bad = "I built it with Flask and Kubernetes, cut wait time by 40% for 300 students at Google."
    assert factcheck.unsupported(ok, resume, answer) == []
    missing = factcheck.unsupported(bad, resume, answer)
    assert "40" in missing and "Kubernetes" in missing and "Google" in missing
    assert "300" not in missing and "Flask" not in missing


def test_factcheck_ignores_placeholders_and_sentence_starts():
    resume = "Built a Flask app."
    assert factcheck.unsupported("First, I built a Flask app. Then it served [number of users] users.", resume) == []


def test_overall_weights():
    assert scoring.overall({"content": 80, "fluency": 60, "delivery": 40}) == 65
    # typed answer: no delivery, re-weighted over content and fluency
    assert scoring.overall({"content": 90, "fluency": 60, "delivery": None}) == 80


def test_content_score_from_rubric():
    rubric = {k: {"score": 3, "reason": ""} for k in ("answered", "structure", "specifics", "jd_fit")}
    assert scoring.content(rubric) == 100
    rubric["specifics"]["score"] = 0
    assert scoring.content(rubric) == 75


def test_delivery_notes():
    score, notes = scoring.delivery({"speech_s": 60, "long_pauses": 10, "trailing_off_db": 9,
                                     "pitch_variation_st": 0.8, "loudness_db": -20}, {"wpm": 90})
    assert score < 60
    text = " ".join(notes)
    assert "slow" in text and "drops" in text and "flat" in text


def test_polish_adds_nothing_new():
    text = "Um so myself Ananya. Basically I built the the app, you know, and I am having good knowledge of SQL."
    out = language.polish(text)
    assert out == "So I'm Ananya. I built the app, and I have good knowledge of SQL."
    assert factcheck.unsupported(out, text) == []
    assert "I am having lunch" in language.polish("I am having lunch")


def test_hesitations_count_as_fillers():
    result = language.analyse("I built it and deployed it", seconds=60, hesitations=3)
    assert result["filler_count"] == 0 and result["hesitations"] == 3 and result["fillers_per_min"] == 3.0
    score, notes = scoring.fluency({**result, "fillers": {}}, spoken=True)
    assert any("umm" in n for n in notes)
