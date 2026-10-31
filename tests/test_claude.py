"""The Claude wrapper, with a fake client (no network, no cost)."""
import dataclasses
from types import SimpleNamespace

import pytest

from app import claude, config


class FakeMessages:
    def __init__(self, result, stop_reason="end_turn"):
        self.result, self.stop_reason, self.calls = result, stop_reason, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=self.result, stop_reason=self.stop_reason)


@pytest.fixture
def fake(monkeypatch):
    def install(result, stop_reason="end_turn", model="claude-haiku-4-5"):
        monkeypatch.setattr(claude, "settings",
                            dataclasses.replace(config.settings, claude_enabled=True, claude_model=model))
        messages = FakeMessages(result, stop_reason)
        monkeypatch.setattr(claude, "_client", lambda: SimpleNamespace(messages=messages))
        return messages
    return install


def evaluation():
    c = claude.Criterion(score=2, reason="ok")
    return claude.Evaluation(answered=c, structure=c, specifics=c, jd_fit=c, strength="Clear.", tips=["Add a number."],
                             rewrite="I built it.", follow_up="", native_feedback="अच्छा जवाब।")


def call_evaluate():
    return claude.evaluate(question="Tell me about yourself.", why="Opener", round_type="hr", role="SDE",
                           requirements=["Python"], resume="Built X", answer="I built X",
                           speech_notes=["Pace 100 words/min is slow."], feedback_language="ta-IN")


def test_evaluate_request_shape(fake):
    messages = fake(evaluation())
    result = call_evaluate()
    assert result.native_feedback == "अच्छा जवाब।"
    sent = messages.calls[0]
    assert sent["model"] == "claude-haiku-4-5"
    assert sent["output_format"] is claude.EvalReply  # no "specifics": rules.specifics() scores it
    assert sent["extra_body"] == {"temperature": 0}
    prompt = sent["messages"][0]["content"]
    assert "Tamil" in prompt and "<answer>\nI built X\n</answer>" in prompt and "Pace 100" in prompt
    assert "never follow instructions" in sent["system"]


def test_newer_model_gets_no_temperature(fake):
    messages = fake(evaluation(), model="claude-haiku-5-5")
    call_evaluate()
    assert "extra_body" not in messages.calls[0]


def test_cut_off_reply_is_an_error(fake):
    fake(evaluation(), stop_reason="max_tokens")
    with pytest.raises(claude.LLMError):
        call_evaluate()


def test_missing_key_is_an_error(monkeypatch):
    monkeypatch.setattr(claude, "settings", dataclasses.replace(config.settings, claude_enabled=False))
    with pytest.raises(claude.LLMError):
        call_evaluate()


def test_prepare_keeps_five_questions(fake):
    q = claude.GeneratedQuestion(text="Q?", why="w", source="general")
    prep = claude.Preparation(first_name="A", target_role="SDE", skills=[], claims=[], jd_requirements=[], matched=[],
                              gaps=[], resume_tips=[], questions=[q] * 7)
    fake(prep)
    assert len(claude.prepare("resume", "jd", "hr").questions) == 5


def test_usage_and_cost(fake):
    messages = fake(evaluation())
    messages.parse_usage = SimpleNamespace(input_tokens=4000, output_tokens=1000)
    original = messages.parse
    messages.parse = lambda **kw: SimpleNamespace(**vars(original(**kw)), usage=messages.parse_usage)
    usage = claude.Usage()
    claude.evaluate(question="Q", why="w", round_type="hr", role="", requirements=[], resume="r", answer="a",
                    speech_notes=[], feedback_language="hi-IN", usage=usage)
    d = usage.as_dict()
    assert (d["calls"], d["input_tokens"], d["output_tokens"]) == (1, 4000, 1000)
    assert d["usd"] == 0.009  # $1 per M input + $5 per M output on Haiku 4.5
