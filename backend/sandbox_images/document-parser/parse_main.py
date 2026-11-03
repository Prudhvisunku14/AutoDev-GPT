"""The document-parser container's entry point: `python -I -B /opt/parser/parse_main.py <kind> /workspace/input.bin`.

NOT DOCKER-TESTED: the image does not exist. It runs app/rag/parsing.py (copied beside it as parsing.py), prints
{"sections": [{"location", "text"}, ...]} on stdout and exits 0, or prints {"refused": reason} and exits 2. It writes
nothing, and the container has no network. The host checks the answer again (app/rag/parsers.py: validate_output).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # parsing.py sits beside this file, in the image; -I leaves it off the path

from parsing import KINDS, ParseRefused, parse_document  # noqa: E402

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_CHARS = 500_000
REFUSED = 2


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in KINDS:
        print(json.dumps({"refused": "usage: parse_main.py <txt|google-doc|docx> <file>"}))
        return REFUSED
    try:
        with open(argv[2], "rb") as handle:
            data = handle.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ParseRefused("the document is over the parser's input limit")
        sections = parse_document(argv[1], data)
        if sum(len(text) for _, text in sections) > MAX_CHARS:
            raise ParseRefused("the document has more text than the parser returns")
    except (ParseRefused, OSError) as exc:
        print(json.dumps({"refused": str(exc) if isinstance(exc, ParseRefused) else "the document could not be read"}))
        return REFUSED
    print(json.dumps({"sections": [{"location": location, "text": text} for location, text in sections]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
