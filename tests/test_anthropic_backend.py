"""Experimental Anthropic backend: mocked client only, no network, no API key needed."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from field_report.backends import BackendError, BackendUnavailable, anthropic_backend as ab
from field_report.backends.classify import Unit, get_classifier
from field_report.backends.vision import get_captioner
from field_report.lib import profiles
from field_report.stages import s5a_pre_label, s6_classify

PROFILE = profiles.load("site-inspection")
CAT = PROFILE["categories"][0]["name"]
RATING = PROFILE["scale"]["values"][0]


def tool_use(name, data, id_="tu_1", stop="tool_use"):
    block = SimpleNamespace(type="tool_use", name=name, id=id_, input=data)
    return SimpleNamespace(stop_reason=stop, content=[block])


class FakeClient:
    """Replays queued responses and records every request. Never touches the network."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=repr)))
        return self.queue.pop(0)


def good_class(**over):
    d = {"category": CAT, "rating": RATING, "confidence": 0.8, "evidence": ["text"]}
    d.update(over)
    return tool_use("record_classification", d)


def good_photo(**over):
    d = {"caption": "A diagonal crack across a plastered wall.", "category": CAT, "rating": RATING,
         "visible_damage": True}
    d.update(over)
    return tool_use("record_photo", d)


UNIT = Unit(cluster_id="c1", text="Diagonal crack in the wall.", captions=["Wall crack"])


def classifier(client):
    return ab.AnthropicClassifier(PROFILE, client=client)


def test_valid_classification_is_parsed():
    c = FakeClient(good_class(evidence=["text", "caption:0"]))
    res = classifier(c).classify(UNIT)
    assert res.category_id == PROFILE["categories"][0]["id"] and res.rating == RATING
    assert res.confidence == 0.8 and len(c.calls) == 1
    assert c.calls[0]["tool_choice"] == {"type": "tool", "name": "record_classification"}


def test_default_and_env_model(monkeypatch):
    monkeypatch.delenv("FRP_ANTHROPIC_MODEL", raising=False)
    assert classifier(FakeClient()).info()["model"] == "claude-haiku-5-5"
    monkeypatch.setenv("FRP_ANTHROPIC_MODEL", "some-other-model")
    assert classifier(FakeClient()).info()["model"] == "some-other-model"


def test_end_turn_with_a_tool_block_is_ok():
    resp = good_class()
    resp.stop_reason = "end_turn"
    assert classifier(FakeClient(resp)).classify(UNIT).category_id is not None


def test_unclear_category_is_allowed_and_means_unclassified():
    res = classifier(FakeClient(good_class(category="unclear"))).classify(UNIT)
    assert res.category_id is None and "unclear" in res.basis


@pytest.mark.parametrize("bad", [
    {"category": "Not A Category"},
    {"rating": "Catastrophic"},
    {"evidence": ["caption:7"]},
    {"evidence": []},
    {"confidence": 3},
])
def test_invalid_then_valid_is_retried_with_the_error_fed_back(bad):
    c = FakeClient(good_class(**bad), good_class())
    res = classifier(c).classify(UNIT)
    assert res.category_id is not None and len(c.calls) == 2
    msgs = c.calls[1]["messages"]
    assert msgs[-2]["role"] == "assistant"
    result = msgs[-1]["content"][0]
    assert result["type"] == "tool_result" and result["is_error"] is True
    assert "Validation failed" in result["content"]


def test_three_invalid_answers_is_a_structured_error():
    c = FakeClient(*[good_class(category="nope") for _ in range(3)], good_class())
    with pytest.raises(ab.AnthropicOutputError) as ei:
        classifier(c).classify(UNIT)
    assert len(c.calls) == 3          # first try + 2 retries, then stop
    err = ei.value.error
    assert err["code"] == "INVALID_OUTPUT" and err["attempts"] == 3 and err["validation_errors"]
    assert isinstance(ei.value, BackendUnavailable)


def test_missing_tool_block_is_retried():
    text_only = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Structure")])
    c = FakeClient(text_only, good_class())
    assert classifier(c).classify(UNIT).category_id is not None and len(c.calls) == 2


def test_max_tokens_is_failure_without_retry_even_if_content_looks_fine():
    truncated = SimpleNamespace(stop_reason="max_tokens", content=good_class().content)
    c = FakeClient(truncated, good_class())
    with pytest.raises(ab.AnthropicOutputError) as ei:
        classifier(c).classify(UNIT)
    assert ei.value.error["code"] == "MAX_TOKENS" and len(c.calls) == 1


def test_other_stop_reason_is_failure():
    c = FakeClient(SimpleNamespace(stop_reason="refusal", content=[]))
    with pytest.raises(ab.AnthropicOutputError) as ei:
        classifier(c).classify(UNIT)
    assert ei.value.error["code"] == "BAD_STOP_REASON"


def test_api_exception_is_an_item_error():
    class Boom(FakeClient):
        def create(self, **kw):
            raise RuntimeError("down")
    with pytest.raises(BackendError) as ei:
        classifier(Boom()).classify(UNIT)
    assert ei.value.code == "ANTHROPIC_API_ERROR" and ei.value.retryable


# ---- captions ---------------------------------------------------------------------

@pytest.fixture
def photo(tmp_path):
    from PIL import Image
    p = tmp_path / "IMG-1.jpg"
    Image.new("RGB", (8, 8), "grey").save(p)
    return p


def test_caption_sends_base64_image_and_parses(photo):
    c = FakeClient(good_photo())
    cap = ab.AnthropicCaptions(PROFILE, client=c).caption(photo)
    assert cap.caption.startswith("A diagonal crack") and cap.category == CAT and cap.visible_damage is True
    img = c.calls[0]["messages"][0]["content"][0]
    assert img["type"] == "image" and img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/jpeg" and img["source"]["data"]


def test_caption_unclear_maps_to_no_category_and_null_rating_allowed(photo):
    c = FakeClient(good_photo(category="unclear", rating=None))
    cap = ab.AnthropicCaptions(PROFILE, client=c).caption(photo)
    assert cap.category is None and cap.rating is None


def test_caption_too_short_is_retried(photo):
    c = FakeClient(good_photo(caption="crack"), good_photo())
    assert ab.AnthropicCaptions(PROFILE, client=c).caption(photo).caption and len(c.calls) == 2


def test_caption_three_invalid_fails(photo):
    c = FakeClient(*[good_photo(category="zzz")] * 3)
    with pytest.raises(ab.AnthropicOutputError):
        ab.AnthropicCaptions(PROFILE, client=c).caption(photo)


def test_unsupported_image_type(tmp_path):
    p = tmp_path / "x.bmp"
    p.write_bytes(b"x")
    with pytest.raises(BackendError):
        ab.AnthropicCaptions(PROFILE, client=FakeClient()).caption(p)


# ---- wiring ------------------------------------------------------------------------

def test_no_api_key_is_backend_unavailable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(BackendUnavailable):
        get_captioner("anthropic")
    with pytest.raises(BackendUnavailable):
        get_classifier("anthropic", PROFILE)


def test_factories_accept_injected_client():
    assert get_classifier("anthropic", PROFILE, client=FakeClient()).name == "anthropic"
    assert get_captioner("anthropic", profile=PROFILE, client=FakeClient()).name == "anthropic"


def test_s6_exits_2_with_structured_error_after_three_invalid(fresh_run, monkeypatch, capsys):
    cfg, rd = fresh_run
    client = FakeClient(*[good_class(category="nope") for _ in range(10)])
    monkeypatch.setattr(ab, "_make_client", lambda: client)
    code = s6_classify.main(["--config", str(cfg), "--run-dir", str(rd), "--classify", "anthropic", "--force"])
    assert code == 2
    err = capsys.readouterr().err
    assert "BACKEND UNAVAILABLE" in err and '"INVALID_OUTPUT"' in err
    assert len(client.calls) == 3


def test_s5a_exits_2_on_max_tokens(fresh_run, monkeypatch):
    cfg, rd = fresh_run
    truncated = SimpleNamespace(stop_reason="max_tokens", content=[])
    client = FakeClient(*[truncated] * 20)
    monkeypatch.setattr(ab, "_make_client", lambda: client)
    code = s5a_pre_label.main(["--config", str(cfg), "--run-dir", str(rd), "--captions", "anthropic",
                               "--force"])
    assert code == 2 and len(client.calls) == 1


def test_s6_with_valid_mock_succeeds(fresh_run, monkeypatch):
    cfg, rd = fresh_run
    client = FakeClient()
    client.create = lambda **kw: good_class(
        evidence=["text"] if "text:" in kw["messages"][0]["content"][0]["text"] else ["caption:0"])
    monkeypatch.setattr(ab, "_make_client", lambda: client)
    code = s6_classify.main(["--config", str(cfg), "--run-dir", str(rd), "--classify", "anthropic", "--force"])
    assert code in (0, 3)
    meta = json.loads((rd / "findings.json").read_text(encoding="utf-8"))["meta"]
    assert meta["model"] == "claude-haiku-5-5"
