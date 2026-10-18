"""Pluggable backends: ASR, translation, photo captioning, classification.

Every external capability sits behind a small interface with a deterministic
offline implementation (used by the tests and the offline demo) next to the real
one. The stages never import a vendor SDK; they ask this package for a backend by
name and talk to the interface:

    asr         faster-whisper | sidecar
    translate   whisper | none
    captions    auto | sidecar | none | anthropic (experimental)
    classify    keyword | anthropic (experimental)

Backends raise BackendError for anything the stage should record against one item
(and carry on); BackendUnavailable means the backend cannot run at all, which
the stage turns into a clear exit 2 instead of a traceback.
"""
from __future__ import annotations


class BackendError(Exception):
    """One item could not be processed. Recorded in errors[]; the stage continues."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class BackendUnavailable(Exception):
    """The backend cannot run at all (missing package, model, key). Stage exits 2."""


ASR_BACKENDS = ("faster-whisper", "sidecar")
TRANSLATE_BACKENDS = ("whisper", "none")
CAPTION_BACKENDS = ("auto", "sidecar", "none", "anthropic")
CLASSIFY_BACKENDS = ("keyword", "anthropic")
