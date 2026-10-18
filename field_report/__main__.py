"""`python -m field_report <command>` - one entry point for the whole pipeline.

    demo           the whole pipeline on the synthetic sample:  demo --offline [--profile ...]
    run            chain the stages for one site (s1 .. s10)
    stage          run a single stage module:  stage s6_classify --config ...
    review         serve the browser review app (the human gate, s5b)
    ingest-review  read a filled review-sheet.docx back into findings.json
    make-sample    generate the synthetic sample (examples/make_sample.py)
"""
from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "demo": "field_report.demo",
    "run": "field_report.run",
    "ingest-review": "field_report.stages.ingest_confirmations",
    "review": "field_report.stages.s5b_review",
}
STAGE_PREFIX = "field_report.stages."


def usage() -> str:
    return ("usage: python -m field_report <command> [options]\n"
            "  demo      the whole pipeline on the synthetic sample (python -m field_report demo --offline)\n"
            "  run       chain the stages for one site           (python -m field_report run --help)\n"
            "  stage     run one stage module, e.g. stage s6_classify --config <config>\n"
            "  review    serve the browser review app (127.0.0.1 only)\n"
            "  ingest-review  read a filled review-sheet.docx back (--config <config> --from-docx <file>)\n"
            "  make-sample    generate the synthetic sample (see examples/make_sample.py --help)\n")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(usage())
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "stage":
        if not rest:
            print(usage(), file=sys.stderr)
            return 2
        module, rest = rest[0], rest[1:]
        try:
            mod = importlib.import_module(STAGE_PREFIX + module)
        except ImportError:
            print(f"unknown stage {module!r}", file=sys.stderr)
            return 2
        return int(mod.main(rest) or 0)
    if cmd == "make-sample":
        import subprocess
        from field_report.lib.common import REPO
        script = REPO / "examples" / "make_sample.py"
        if not script.is_file():
            print("examples/make_sample.py not found (run from a source checkout)", file=sys.stderr)
            return 2
        return subprocess.call([sys.executable, str(script), *rest])
    if cmd in COMMANDS:
        mod = importlib.import_module(COMMANDS[cmd])
        return int(mod.main(rest) or 0)
    print(f"unknown command {cmd!r}\n" + usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
