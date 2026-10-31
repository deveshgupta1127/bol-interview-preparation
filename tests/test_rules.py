"""Deterministic rules (app/rules.py) and how they replace parts of Claude's job."""
import dataclasses
from types import SimpleNamespace

import pytest

from app import claude, config, rules


# ---------- skills dictionary ----------

def test_skills_found_in_order():
    text = "Built REST APIs in Java with Spring Boot; React and Node.js front end; C++ and C# too"
    assert rules.find_skills(text) == ["REST APIs", "Java", "Spring Boot", "React", "Node.js", "C++", "C#"]


@pytest.mark.parametrize("text", ["I react calmly to feedback", "spring semester project", "express delivery",
                                  "Go to the market", "Lund University", "B.Tech in C and R"])
def test_ordinary_words_are_not_skills(text):
    assert rules.find_skills(text) == []


def test_lookalikes_are_kept_apart():
    assert rules.find_skills("JavaScript only") == ["JavaScript"]  # not Java
    assert rules.find_skills("GitHub") == ["GitHub"]  # not Git
    assert rules.find_skills("React Native app") == ["React Native"]  # not React as well


# ---------- claim strength ----------

@pytest.mark.parametrize("claim, strength", [
    ("Built a canteen app used by 300 students", "specific"),
    ("Increased sales by twenty percent", "specific"),
    ("Cut report time by half", "specific"),
    ("Led the college hackathon team", "vague"),
    ("B.Tech CSE 2026, built a website", "vague"),  # a year is not a scale
])
def test_claim_strength(claim, strength):
    assert rules.claim_strength(claim) == strength


# ---------- specifics score ----------

@pytest.mark.parametrize("answer, score", [
    ("I am a good student and I like coding a lot", 0),
    ("In my second year we organised a fest at our college and it went well", 1),
    ("I worked at Infosys as an intern on their payroll team and learnt a lot", 2),
    ("So basically I built a canteen ordering app in Flask and MySQL for 300 students.", 3),
    ("मैंने Flask में एक app बनाया जो 300 students use करते थे और queue कम हुई", 3),
    ("too short", 0),
    # found in the 31 Oct check: hopes aren't results ("grow my career" used to count)
    ("I like playing cricket on weekends and I want to join a good company and grow my career", 0),
    ("We launched the app in March and the lunch queue came down a lot for everyone", 2),
])
def test_specifics_score(answer, score):
    assert rules.specifics(answer)[0] == score


def test_specifics_reason_says_what_is_missing():
    score, reason = rules.specifics("I worked at Infosys as an intern on their payroll team and learnt a lot")
    assert score == 2 and "add a number or result" in reason


def test_same_answer_same_score():
    answer = "I led a team of 4 and we built the app in React for 200 users"
    assert len({rules.specifics(answer) for _ in range(5)}) == 1


# ---------- wired into Claude's jobs ----------

@pytest.fixture
def fake(monkeypatch):
    def install(result):
        monkeypatch.setattr(claude, "settings", dataclasses.replace(config.settings, claude_enabled=True))
        sent = []
        messages = SimpleNamespace(parse=lambda **kw: sent.append(kw) or SimpleNamespace(
            parsed_output=result, stop_reason="end_turn"))
        monkeypatch.setattr(claude, "_client", lambda: SimpleNamespace(messages=messages))
        return sent
    return install


def test_prepare_uses_rules_for_opener_skills_and_claims(fake):
    q = claude.GeneratedQuestion(text="What was your part in the canteen app?", why="w", source="resume_claim")
    reply = claude.PrepReply(first_name="Ananya", target_role="SDE",
                             claims=["Built a canteen app for 300 students", "Led the hackathon team"],
                             jd_requirements=["Python"], matched=["Python"], gaps=[], resume_tips=[],
                             questions=[q] * 6)
    sent = fake(reply)
    prep = claude.prepare("Ananya. Python, Flask, MySQL. Built a canteen app for 300 students.", "JD text", "technical")
    assert prep.questions[0].text == rules.OPENERS["technical"][0] and len(prep.questions) == 5
    assert prep.skills == ["Python", "Flask", "MySQL"]
    assert [c.strength for c in prep.claims] == ["specific", "vague"]
    assert sent[0]["output_format"] is claude.PrepReply
    prompt = sent[0]["messages"][0]["content"]
    assert "exactly 4 questions" in prompt and rules.OPENERS["technical"][0] in prompt


def test_opener_is_not_shared_between_sessions(fake):
    q = claude.GeneratedQuestion(text="Q?", why="w", source="resume_claim")
    fake(claude.PrepReply(first_name="", target_role="", claims=[], jd_requirements=[], matched=[], gaps=[],
                          resume_tips=[], questions=[q] * 4))
    claude.prepare("resume", "jd", "hr").questions[0].text = "changed"
    assert claude.OPENERS["hr"].text == rules.OPENERS["hr"][0]


def test_evaluate_scores_specifics_by_rule(fake):
    c = claude.Criterion(score=2, reason="ok")
    sent = fake(claude.EvalReply(answered=c, structure=c, jd_fit=c, strength="s", tips=[], rewrite="r",
                                 follow_up="", native_feedback="n"))
    answer = "So basically I built a canteen ordering app in Flask and MySQL for 300 students."
    ev = claude.evaluate(question="Q", why="w", round_type="hr", role="", requirements=[], resume="r", answer=answer,
                         speech_notes=[], feedback_language="hi-IN")
    assert ev.specifics.score == 3 and "Flask" in ev.specifics.reason
    prompt = sent[0]["messages"][0]["content"]
    assert "scored separately: 3/3" in prompt and "- specifics:" not in prompt


def test_stand_in_uses_the_same_rules():
    answer = "I worked at Infosys as an intern on their payroll team and learnt a lot"
    ev = claude.stub_evaluate(answer=answer, resume="", requirements=[], feedback_language="hi-IN")
    assert (ev.specifics.score, ev.specifics.reason) == rules.specifics(answer)
    prep = claude.stub_prepare("Ananya\nBuilt a canteen ordering app in Flask for 300 students\n", "Python, SQL", "hr")
    assert prep.questions[0].text == rules.OPENERS["hr"][0]
