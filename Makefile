# Thin wrapper: the canonical entry point is `python -m field_report ...` (works on Windows without make).
PY ?= python

.PHONY: demo demo-offline test lint

# Real faster-whisper (tiny, CPU int8); one-time ~75 MB model download.
demo:
	$(PY) -m field_report demo --out out/demo

# No model, no network: transcripts and captions come from the sample's sidecar files.
demo-offline:
	$(PY) -m field_report demo --offline --out out/demo

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .
