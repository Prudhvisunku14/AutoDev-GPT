"""Records the test runs ReplaySandbox answers with: `python -m app.demo.record`.

It runs the sandbox's approved unittest command with this machine's Python, once per tree in
`app.demo.fixtures.TREES`, and nothing else: it takes no path and no argument, so it can never run generated
code. The files are repository fixtures; this is what tests/unit/test_verification.py already does for its own
fixture files. Paths in the output are rewritten to /workspace, where the sandbox mounts the project.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.demo.fixtures import TREES
from app.sandbox.policy import APPROVED_COMMANDS
from app.sandbox.replay import RECORDINGS_FILE, recording_key
from app.sandbox.spec import ExecStatus

COMMAND = APPROVED_COMMANDS["python-unittest"]


def run_tree(files: dict[str, bytes]) -> dict[str, Any]:
    """The approved command's arguments with this interpreter in place of the image's `python`."""
    with tempfile.TemporaryDirectory(prefix="devpilot-record-") as folder:
        root = Path(folder)
        for rel, data in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
        done = subprocess.run([sys.executable, *COMMAND.argv[1:]], cwd=root, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)

        def tidy(text: str) -> str:
            return text.replace(str(root) + "\\", "/workspace/").replace(str(root) + "/", "/workspace/")

        return {"status": (ExecStatus.SUCCEEDED if done.returncode == 0 else ExecStatus.FAILED).value, "exit_code": done.returncode, "stdout": tidy(done.stdout), "stderr": tidy(done.stderr)}


def record() -> dict[str, Any]:
    recordings = [{"name": name, "key": recording_key(COMMAND.name, COMMAND.argv, files), **run_tree(files)} for name, files in TREES.items()]
    return {
        "how": "The approved python-unittest command, run with the host's Python over repository fixtures. Not a Docker run.",
        "python": platform.python_version(),
        "recordings": recordings,
    }


def main() -> int:
    data = record()
    RECORDINGS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    for r in data["recordings"]:
        print(f"{r['status']:<9} exit {r['exit_code']}  {r['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
