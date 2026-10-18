"""EXPERIMENTAL Claude backends for photo captions and classification.

Optional extra (`pip install field-report-pipeline[anthropic]`); needs ANTHROPIC_API_KEY.
Model: FRP_ANTHROPIC_MODEL (default `claude-haiku-5-5`). Nothing here runs in the default
offline flow or in the default tests (they inject a mocked client).

Contract with the model: a forced tool call whose input is a JSON object. The loop ends on
`stop_reason`, never on reply text:

    tool_use / end_turn   look for the tool_use block and validate it
    max_tokens            failure (truncated output is never parsed)
    anything else         failure (refusal, pause_turn, ...)

The JSON-schema tool input is then validated *in code* (category in profile or "unclear",
rating in the profile scale, evidence references exist). On a validation error the error text
is fed back to the model and the call is retried, at most MAX_RETRIES (2) times. Persistent
invalid output raises AnthropicOutputError (a BackendUnavailable), which the stage reports as
a structured error and exit 2.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import jsonschema

from field_report.backends import BackendError, BackendUnavailable
from field_report.backends.classify import Classification, Unit
from field_report.backends.vision import MIN_CAPTION_CHARS, Caption

DEFAULT_MODEL = "claude-haiku-5-5"
PROMPT_VERSION = "anthropic-1.0.0"
MAX_RETRIES = 2
MAX_TOKENS = 1024
UNCLEAR = "unclear"
_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp"}


class AnthropicOutputError(BackendUnavailable):
    """The model could not produce valid output. Carries a structured `error` dict."""

    def __init__(self, code: str, message: str, attempts: int, last_errors: list[str] | None = None):
        super().__init__(f"{code}: {message}")
        self.error = {"code": code, "message": message, "attempts": attempts,
                      "validation_errors": last_errors or []}


def _make_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise BackendUnavailable("ANTHROPIC_API_KEY is not set (see .env.example)")
    try:
        import anthropic
    except ImportError as e:
        raise BackendUnavailable("the 'anthropic' package is not installed "
                                 "(pip install field-report-pipeline[anthropic])") from e
    return anthropic.Anthropic()


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def call_with_schema(client, model: str, system: str, user_content: list, tool: dict, validate,
                     max_retries: int = MAX_RETRIES):
    """Forced tool call -> schema check -> semantic check -> bounded retry. Returns the tool input."""
    messages = [{"role": "user", "content": user_content}]
    errors: list[str] = []
    attempts = 0
    while True:
        attempts += 1
        try:
            resp = client.messages.create(
                model=model, max_tokens=MAX_TOKENS, system=system, messages=messages,
                tools=[tool], tool_choice={"type": "tool", "name": tool["name"]})
        except Exception as e:  # noqa: BLE001 - SDK errors vary; surface as one item error
            raise BackendError("ANTHROPIC_API_ERROR", f"{type(e).__name__}: {e}", retryable=True) from e
        stop = _field(resp, "stop_reason")
        if stop == "max_tokens":
            raise AnthropicOutputError("MAX_TOKENS", "model output was truncated (stop_reason=max_tokens)",
                                       attempts)
        if stop not in ("tool_use", "end_turn"):
            raise AnthropicOutputError("BAD_STOP_REASON", f"unexpected stop_reason {stop!r}", attempts)
        content = list(_field(resp, "content", []) or [])
        block = next((b for b in content if _field(b, "type") == "tool_use"
                      and _field(b, "name") == tool["name"]), None)
        if block is None:
            errors = [f"you must answer by calling the {tool['name']} tool"]
        else:
            data = _field(block, "input")
            try:
                jsonschema.validate(data, tool["input_schema"])
                errors = validate(data)
            except jsonschema.ValidationError as e:
                errors = [f"schema: {e.message}"]
            if not errors:
                return data
        if attempts > max_retries:
            raise AnthropicOutputError("INVALID_OUTPUT", "model output failed validation after "
                                       f"{max_retries} retries", attempts, errors)
        # feed the error back: echo the assistant turn, answer the tool_use with the error
        messages.append({"role": "assistant", "content": content})
        text = "Validation failed: " + "; ".join(errors) + ". Call the tool again with a corrected input."
        if block is not None:
            messages.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": _field(block, "id"), "is_error": True,
                "content": text}]})
        else:
            messages.append({"role": "user", "content": text})


def _names(profile: dict) -> list[str]:
    return [c["name"] for c in profile["categories"]]


def _caption_tool(profile: dict) -> dict:
    return {"name": "record_photo", "description": "Record what the photo shows.",
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "required": ["caption", "category", "rating", "visible_damage"],
                "properties": {
                    "caption": {"type": "string", "description": "One factual sentence, what is visible."},
                    "category": {"type": "string", "enum": _names(profile) + [UNCLEAR]},
                    "rating": {"enum": list(profile["scale"]["values"]) + [None]},
                    "visible_damage": {"type": "boolean"}}}}


class AnthropicCaptions:
    name = "anthropic"

    def __init__(self, profile: dict | None = None, client=None, model: str | None = None) -> None:
        from field_report.lib import profiles
        self.profile = profile or profiles.load("site-inspection")
        self.client = client or _make_client()
        self.model = model or os.environ.get("FRP_ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.tool = _caption_tool(self.profile)

    def info(self) -> dict:
        return {"model": self.model, "prompt_version": PROMPT_VERSION}

    @staticmethod
    def _validate(data: dict) -> list[str]:
        if len(data["caption"].strip()) < MIN_CAPTION_CHARS:
            return [f"caption must be at least {MIN_CAPTION_CHARS} characters"]
        return []

    def caption(self, path: Path) -> Caption:
        mt = _MEDIA_TYPES.get(path.suffix.lower())
        if mt is None:
            raise BackendError("UNSUPPORTED_IMAGE", f"{path.name}: unsupported image type")
        try:
            b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        except OSError as e:
            raise BackendError("IMAGE_UNREADABLE", f"{path.name}: {e}") from e
        system = ("You describe site photos for an inspection report. State only what is visible; "
                  "use category 'unclear' when the photo does not clearly belong to one category. "
                  "Categories: " + ", ".join(_names(self.profile)) + ".")
        content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}},
                   {"type": "text", "text": "Describe this photo by calling record_photo."}]
        data = call_with_schema(self.client, self.model, system, content, self.tool, self._validate)
        cat = None if data["category"] == UNCLEAR else data["category"]
        return Caption(caption=data["caption"].strip(), category=cat, rating=data["rating"],
                       visible_damage=data["visible_damage"])


def _classify_tool(profile: dict) -> dict:
    return {"name": "record_classification", "description": "Record the category and rating of one unit.",
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "required": ["category", "rating", "confidence", "evidence"],
                "properties": {
                    "category": {"type": "string", "enum": _names(profile) + [UNCLEAR]},
                    "rating": {"type": "string", "enum": list(profile["scale"]["values"])},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                                 "description": "References like 'text' or 'caption:0' supporting the answer."}}}}


class AnthropicClassifier:
    name = "anthropic"

    def __init__(self, profile: dict, client=None, model: str | None = None) -> None:
        self.profile = profile
        self.client = client or _make_client()
        self.model = model or os.environ.get("FRP_ANTHROPIC_MODEL") or DEFAULT_MODEL
        self.tool = _classify_tool(profile)
        self._ids = {c["name"]: c["id"] for c in profile["categories"]}

    def info(self) -> dict:
        return {"model": self.model, "prompt_version": PROMPT_VERSION}

    def classify(self, unit: Unit) -> Classification:
        refs = {f"caption:{i}" for i in range(len(unit.captions))}
        if unit.text.strip():
            refs.add("text")

        def validate(data: dict) -> list[str]:
            bad = [e for e in data["evidence"] if e not in refs]
            if bad:
                return [f"evidence reference(s) {bad} do not exist; valid references: {sorted(refs)}"]
            return []

        lines = [f"text: {unit.text}"] if unit.text.strip() else []
        lines += [f"caption:{i}: {c}" for i, c in enumerate(unit.captions)]
        system = ("You classify one field observation into a category and a rating. Answer only by calling "
                  "record_classification. Use category 'unclear' if the evidence does not support one. "
                  f"Rating scale ({self.profile['scale'].get('label', 'rating')}): "
                  + ", ".join(self.profile["scale"]["values"]) + ".")
        content = [{"type": "text", "text": "Evidence:\n" + ("\n".join(lines) or "(none)")}]
        data = call_with_schema(self.client, self.model, system, content, self.tool, validate)
        cid = self._ids.get(data["category"])  # "unclear" -> None -> listed as unclassified by s6
        basis = f"model: {data['category']}; evidence {', '.join(data['evidence'])}"
        return Classification(cid, data["rating"], float(data["confidence"]), basis)


def get_captioner(profile: dict | None = None, client=None, model: str | None = None, **_):
    return AnthropicCaptions(profile=profile, client=client, model=model)


def get_classifier(profile: dict, client=None, model: str | None = None, **_):
    return AnthropicClassifier(profile, client=client, model=model)
