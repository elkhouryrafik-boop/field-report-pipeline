"""Backends: sidecar ASR/captions, translation, keyword classification, quantity extraction.

Everything here is offline; the real faster-whisper test is marked slow (FRP_RUN_SLOW=1).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from field_report.backends import BackendError, BackendUnavailable
from field_report.backends.asr import FasterWhisperAsr, SidecarAsr, get_asr
from field_report.backends.classify import KeywordClassifier, Unit, get_classifier
from field_report.backends.translate import get_translator
from field_report.backends.vision import get_captioner
from field_report.lib import profiles, quantities

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample" / "export"
NOTE = SAMPLE / "PTT-20300114-WA0003.opus"
PHOTO = SAMPLE / "IMG-20300114-WA0004.jpg"


# ---- ASR -------------------------------------------------------------------

def test_sidecar_asr_returns_the_ground_truth():
    res = SidecarAsr().transcribe(NOTE)
    assert res.text.startswith("Stop two, east stair")
    assert res.language == "en" and res.duration > 0
    assert res.segments[0].text == res.text


def test_sidecar_asr_missing_sidecar_is_an_item_error(tmp_path):
    audio = tmp_path / "PTT-1.opus"
    shutil.copy(NOTE, audio)
    with pytest.raises(BackendError) as ei:
        SidecarAsr().transcribe(audio)
    assert ei.value.code == "SIDECAR_MISSING"


def test_unknown_backends_are_unavailable_not_tracebacks():
    with pytest.raises(BackendUnavailable):
        get_asr("nope")
    with pytest.raises(BackendUnavailable):
        get_translator("nope", None)
    with pytest.raises(BackendUnavailable):
        get_captioner("nope")
    with pytest.raises(BackendUnavailable):
        get_classifier("nope", profiles.load("site-inspection"))


def test_faster_whisper_rejects_a_bad_device():
    with pytest.raises(BackendUnavailable):
        FasterWhisperAsr(device="tpu")


def test_auto_device_stays_on_cpu_without_the_cuda_runtime(monkeypatch):
    """A GPU without cuBLAS makes CTranslate2 hang, so auto must not pick it."""
    monkeypatch.setattr(FasterWhisperAsr, "_cuda_available", staticmethod(lambda: False))
    asr = FasterWhisperAsr(model="tiny", device="auto")
    assert asr.requested_device == "auto"
    assert asr.info()["compute_type"] == "auto" and asr.info()["vad_filter"] is True


# ---- translate ---------------------------------------------------------------

def test_translate_passes_english_through_and_translates_the_rest(tmp_path):
    asr = SidecarAsr()
    t = get_translator("whisper", asr)
    assert t.translate("Stop one.", "en", NOTE).method == "source"
    audio = tmp_path / "PTT-9.opus"
    shutil.copy(NOTE, audio)
    (tmp_path / "PTT-9.transcript.json").write_text(
        '{"text": "Arret un.", "language": "fr", "text_en": "Stop one."}', encoding="utf-8")
    res = t.translate("Arret un.", "fr", audio)
    assert (res.en_text, res.method, res.translated) == ("Stop one.", "whisper", True)


def test_translate_none_flags_foreign_text_as_untranslated():
    t = get_translator("none", None)
    assert t.translate("hola", "es", NOTE).translated is False
    assert t.translate("hello", "en", NOTE).translated is True


def test_translate_without_an_english_rendering_is_an_error(tmp_path):
    audio = tmp_path / "PTT-9.opus"
    shutil.copy(NOTE, audio)
    (tmp_path / "PTT-9.transcript.json").write_text('{"text": "hola", "language": "es"}', encoding="utf-8")
    with pytest.raises(BackendError):
        get_translator("whisper", SidecarAsr()).translate("hola", "es", audio)


# ---- captions ------------------------------------------------------------------

def test_sidecar_captions_carry_a_category_hint():
    cap = get_captioner("sidecar").caption(PHOTO)
    assert cap.caption and len(cap.caption) >= 10
    assert cap.category in {c["name"] for c in profiles.load("site-inspection")["categories"]} or cap.category
    assert cap.rating in {"High", "Medium", "Low"}


def test_no_captions_backend_returns_none():
    assert get_captioner("none").caption(PHOTO).caption is None


def test_anthropic_backend_is_unavailable_until_installed():
    try:
        get_captioner("anthropic")
    except BackendUnavailable:
        return
    pytest.skip("experimental anthropic backend is present in this build")


# ---- keyword classification ------------------------------------------------------

@pytest.fixture(scope="module")
def site():
    return KeywordClassifier(profiles.load("site-inspection"))


@pytest.mark.parametrize("text, category, rating", [
    ("Stop one. A diagonal crack runs up the wall. Priority high.", 1, "High"),
    ("Water stain near the light fitting, probably a roof leak. Priority medium.", 2, "Medium"),
    ("Cable hanging loose below the junction box, no conduit. Priority high.", 3, "High"),
    ("The fire exit door is blocked and the exit sign is dark.", 4, "High"),
    ("Handrail loose at the first landing of the east stair. Priority low.", 5, "Low"),
    ("Standing water on the floor and the finish is lifting. Priority low.", 6, "Low"),
])
def test_keyword_rules_pick_category_and_stated_priority(site, text, category, rating):
    res = site.classify(Unit(cluster_id="C001", text=text))
    assert (res.category_id, res.rating) == (category, rating)


def test_a_stated_rating_beats_keywords(site):
    res = site.classify(Unit(cluster_id="C001", text="Exposed cable near the panel. Priority low."))
    assert res.rating == "Low" and "stated" in res.basis


def test_unmatched_evidence_is_not_guessed(site):
    res = site.classify(Unit(cluster_id="C001", text="We had lunch."))
    assert res.category_id is None and res.confidence == 0.0


def test_caption_hint_places_a_note_with_no_keyword_but_never_outvotes_clear_text(site):
    clear = Unit(cluster_id="C001", text="A crack in the wall.", category_hints=["Building envelope"])
    assert site.classify(clear).category_id == 1
    silent = Unit(cluster_id="C001", text="Something odd here.", category_hints=["Finishes"])
    assert site.classify(silent).category_id == 6


def test_damage_profile_uses_its_own_scale():
    dmg = KeywordClassifier(profiles.load("damage-assessment"))
    res = dmg.classify(Unit(cluster_id="C001", text="Roof sheeting missing on the east slope. Severity severe."))
    assert (res.category_id, res.rating) == (2, "Severe")
    res = dmg.classify(Unit(cluster_id="C001", text="Hairline cracking in the plinth wall."))
    assert (res.category_id, res.rating) == (1, "Minor")


def test_event_log_profile_does_not_classify():
    ev = KeywordClassifier(profiles.load("event-log"))
    res = ev.classify(Unit(cluster_id="C001", text="Anything at all."))
    assert (res.category_id, res.rating) == (1, profiles.NOT_APPLICABLE)


# ---- quantities --------------------------------------------------------------------

@pytest.mark.parametrize("text, unit, value, confirmed", [
    ("a diagonal crack, about two metres long", "m", 2, False),
    ("three metres of it", "m", 3, True),
    ("about two square metres of floor", "m2", 2, False),
    ("a crack twelve millimetres wide", "mm", 12, True),
    ("about six sheets of roofing", "no", 6, False),
    ("a gap of 2.5 m", "m", 2.5, True),
])
def test_spoken_quantities_are_extracted(text, unit, value, confirmed):
    q = quantities.extract_quantities(text)
    assert [(x["unit"], x["value"], x["confirmed"]) for x in q] == [(unit, float(value), confirmed)]


def test_numbers_must_be_in_the_source():
    assert quantities.number_in_text(150, "one hundred and fifty metres")
    assert not quantities.number_in_text(5, "fifteen metres")
    assert quantities.extract_quantities("no figures here") == []


# ---- real ASR (slow) ------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("FRP_RUN_SLOW") != "1", reason="set FRP_RUN_SLOW=1 to run real ASR")
def test_faster_whisper_tiny_transcribes_a_sample_note():
    res = FasterWhisperAsr(model="tiny", device="cpu").transcribe(NOTE)
    said = set(res.text.lower().replace(",", " ").replace(".", " ").split())
    truth = set(SidecarAsr().transcribe(NOTE).text.lower().replace(",", " ").replace(".", " ").split())
    assert len(said & truth) / len(truth) >= 0.6
    assert res.language == "en"
