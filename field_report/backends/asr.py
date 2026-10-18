"""Speech-to-text backends.

`faster-whisper` is the real one: CPU int8 by default (no GPU, no key), CUDA when
asked for or when `device="auto"` finds one. Voice-activity detection is on and the
language is auto-detected per note. `sidecar` reads `<audio stem>.transcript.json`
next to the audio file - the ground truth shipped with the synthetic sample, used by
the tests and the offline demo so they need neither a model nor a network.

Both return the same `AsrResult`, so nothing downstream knows which one ran.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from field_report.backends import BackendError, BackendUnavailable

DEFAULT_MODEL = "tiny"
MODELS = ("tiny", "base", "small", "medium", "large-v3")


@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str
    no_speech_prob: float | None = None
    avg_logprob: float | None = None

    def as_dict(self) -> dict:
        return {"id": self.id, "start": self.start, "end": self.end, "text": self.text,
                "no_speech_prob": self.no_speech_prob, "avg_logprob": self.avg_logprob}


@dataclass
class AsrResult:
    text: str
    language: str | None
    language_probability: float | None
    duration: float
    segments: list[Segment] = field(default_factory=list)


def audio_duration(path: Path) -> float:
    """Length in seconds via PyAV (ships with faster-whisper); 0.0 if it cannot be read."""
    try:
        import av
        with av.open(str(path)) as container:
            if container.duration:
                return round(container.duration / 1_000_000, 3)
            stream = container.streams.audio[0]
            if stream.duration and stream.time_base:
                return round(float(stream.duration * stream.time_base), 3)
    except Exception:  # noqa: BLE001, S110 - duration is informational
        pass
    return 0.0


class SidecarAsr:
    """Ground-truth transcripts from `<stem>.transcript.json` ({"text","language"[,"text_en"]})."""

    name = "sidecar"

    def __init__(self) -> None:
        self.model = "sidecar"
        self.device = "none"
        self.compute_type = "none"

    def info(self) -> dict:
        return {"model": self.model, "device": self.device, "compute_type": self.compute_type,
                "vad_filter": False, "language": "sidecar"}

    def _read(self, path: Path) -> dict:
        side = path.with_name(path.stem + ".transcript.json")
        if not side.is_file():
            raise BackendError("SIDECAR_MISSING", f"no sidecar transcript next to {path.name}")
        try:
            data = json.loads(side.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise BackendError("SIDECAR_INVALID", f"{side.name} is not valid JSON: {e}") from e
        if not isinstance(data.get("text"), str):
            raise BackendError("SIDECAR_INVALID", f"{side.name} has no 'text' string")
        return data

    def transcribe(self, path: Path) -> AsrResult:
        data = self._read(path)
        text = data["text"].strip()
        dur = audio_duration(path)
        seg = Segment(0, 0.0, dur, text) if text else None
        return AsrResult(text=text, language=data.get("language") or "en", language_probability=1.0,
                         duration=dur, segments=[seg] if seg else [])

    def translate_to_english(self, path: Path) -> str | None:
        """The sidecar's own English rendering, if it ships one."""
        return (self._read(path).get("text_en") or None)


class FasterWhisperAsr:
    """faster-whisper on CPU (int8) or CUDA. The model loads once, on first use."""

    name = "faster-whisper"

    def __init__(self, model: str | None = None, device: str = "auto",
                 compute_type: str | None = None, model_dir: str | None = None) -> None:
        self.model = model or os.environ.get("FRP_ASR_MODEL") or DEFAULT_MODEL
        if device not in ("auto", "cpu", "cuda"):
            raise BackendUnavailable(f"--device must be auto|cpu|cuda, got {device!r}")
        self.requested_device = device
        self.device = device
        self.compute_type = compute_type
        self.model_dir = model_dir or os.environ.get("FRP_MODEL_DIR") or None
        self._model = None
        self.model_load_seconds = 0.0

    def info(self) -> dict:
        return {"model": self.model, "device": self.device,
                "compute_type": self.compute_type or "auto", "vad_filter": True,
                "language": "auto"}

    @staticmethod
    def _cuda_available() -> bool:
        """A CUDA device exists AND the CUDA runtime library CTranslate2 needs can be loaded.

        A GPU without cuBLAS makes CTranslate2 hang inside the first encode instead of failing,
        so `auto` only picks CUDA when the library is really there; otherwise it uses the CPU.
        Ask for `--device cuda` explicitly to skip this check.
        """
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() <= 0:
                return False
        except Exception:  # noqa: BLE001
            return False
        import ctypes
        names = {"win32": ["cublas64_12.dll"], "linux": ["libcublas.so.12"]}.get(sys.platform, [])
        for name in names:
            try:
                ctypes.CDLL(name)
                return True
            except OSError:
                continue
        return False

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise BackendUnavailable(
                "faster-whisper is not installed (pip install faster-whisper), "
                "or use --asr sidecar") from e
        device = self.requested_device
        if device == "auto":
            device = "cuda" if self._cuda_available() else "cpu"
        compute = self.compute_type or ("float16" if device == "cuda" else "int8")
        t0 = time.monotonic()
        try:
            self._model = WhisperModel(self.model, device=device, compute_type=compute,
                                       download_root=self.model_dir)
        except Exception as e:
            raise BackendUnavailable(
                f"could not load faster-whisper model {self.model!r} on {device}: {e}. "
                f"The first run downloads the model; use --asr sidecar to stay offline.") from e
        self.model_load_seconds = round(time.monotonic() - t0, 3)
        self.device, self.compute_type = device, compute
        return self._model

    def transcribe(self, path: Path) -> AsrResult:
        model = self._load()
        try:
            segments, info = model.transcribe(str(path), vad_filter=True, language=None, beam_size=5)
            segs = [Segment(i, round(s.start, 3), round(s.end, 3), s.text.strip(),
                            getattr(s, "no_speech_prob", None), getattr(s, "avg_logprob", None))
                    for i, s in enumerate(segments)]
        except Exception as e:
            raise BackendError("ASR_FAILED", f"{path.name}: {e}", retryable=True) from e
        text = " ".join(s.text for s in segs if s.text).strip()
        return AsrResult(text=text, language=info.language,
                         language_probability=round(float(info.language_probability), 4),
                         duration=round(float(info.duration), 3), segments=segs)

    def translate_to_english(self, path: Path) -> str | None:
        """Whisper's own task="translate": speech in any language -> English text."""
        model = self._load()
        try:
            segments, _ = model.transcribe(str(path), task="translate", vad_filter=True, beam_size=5)
            return " ".join(s.text.strip() for s in segments).strip() or None
        except Exception as e:
            raise BackendError("TRANSLATE_FAILED", f"{path.name}: {e}", retryable=True) from e


def get_asr(name: str, model: str | None = None, device: str = "auto"):
    if name == "sidecar":
        return SidecarAsr()
    if name == "faster-whisper":
        return FasterWhisperAsr(model=model, device=device)
    raise BackendUnavailable(f"unknown ASR backend {name!r}; choose faster-whisper or sidecar")
