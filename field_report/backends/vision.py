"""Photo captioning backends.

`sidecar`    reads `<photo stem>.caption.json` next to each photo - the ground truth the
             synthetic sample ships. It also carries the category / rating the scene was
             built to show, which the classifier uses as a hint (never as an override).
`none`       no captions; the reviewer sees the photo raw and the classifier works from the
             transcript alone.
`anthropic`  experimental Claude vision backend (see backends/anthropic_backend.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from field_report.backends import BackendError, BackendUnavailable

MIN_CAPTION_CHARS = 10  # prelabels.schema.json floor


@dataclass
class Caption:
    caption: str | None
    category: str | None = None      # a category *name* from the profile, as a hint
    rating: str | None = None        # a scale value, as a hint
    finding: str | None = None
    visible_damage: bool | None = None
    countable: dict | None = None    # {"what": str, "n": int}


class SidecarCaptions:
    name = "sidecar"

    def info(self) -> dict:
        return {"model": "sidecar", "prompt_version": "n/a"}

    def caption(self, path: Path) -> Caption:
        side = path.with_name(path.stem + ".caption.json")
        if not side.is_file():
            raise BackendError("SIDECAR_MISSING", f"no sidecar caption next to {path.name}")
        try:
            data = json.loads(side.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise BackendError("SIDECAR_INVALID", f"{side.name} is not valid JSON: {e}") from e
        cap = data.get("caption")
        if not isinstance(cap, str) or len(cap.strip()) < MIN_CAPTION_CHARS:
            raise BackendError("SIDECAR_INVALID", f"{side.name}: caption missing or shorter "
                                                   f"than {MIN_CAPTION_CHARS} characters")
        rating = data.get("priority") or data.get("severity") or data.get("rating")
        return Caption(caption=cap.strip(), category=data.get("category"), rating=rating,
                       finding=data.get("finding"), visible_damage=bool(rating))


class NoCaptions:
    name = "none"

    def info(self) -> dict:
        return {"model": "none", "prompt_version": "n/a"}

    def caption(self, path: Path) -> Caption:
        return Caption(caption=None)


def get_captioner(name: str, **kwargs):
    if name == "sidecar":
        return SidecarCaptions()
    if name == "none":
        return NoCaptions()
    if name == "anthropic":
        try:
            from field_report.backends import anthropic_backend
        except ImportError as e:
            raise BackendUnavailable(
                "the experimental anthropic backend is not available in this build "
                "(pip install field-report-pipeline[anthropic])") from e
        return anthropic_backend.get_captioner(**kwargs)
    raise BackendUnavailable(f"unknown captions backend {name!r}; choose sidecar, none or anthropic")
