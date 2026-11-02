"""The fastapi-pytest recipe's options, checked with the real pytest this repository runs on (9.2.0, the version pinned for
devpilot/verify-fastapi:1) and the image's own pytest.ini, over a fixture this test writes, on the host. The image does
not exist, so this proves what the options mean, not the container. The fixture is trusted code from this file; nothing
generated runs here."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.verification import recipes

IMAGE_CONFIG = Path(recipes.__file__).resolve().parents[2] / "sandbox_images" / "verify-fastapi" / "pytest.ini"
FIXTURE = {
    "backend/app/__init__.py": "",
    "backend/app/main.py": "def answer():\n    return 42\n",
    "backend/tests/test_api.py": "from app.main import answer\n\n\ndef test_ac_001_answer():\n    assert answer() == 42\n",
    # The project's own settings: read, they would silence the output or collect nothing.
    "backend/pytest.ini": "[pytest]\naddopts = -q -p no:terminal\npython_files = nothing_*.py\n",
    "backend/pyproject.toml": "[tool.pytest.ini_options]\naddopts = '--collect-only'\n",
    # A conftest outside the component folder, which must never load.
    "conftest.py": "def pytest_collection_modifyitems(items):\n    raise SystemExit('outside conftest loaded')\n",
}


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    for rel, text in FIXTURE.items():
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text(text, encoding="utf-8")
    return workspace


def _run(argv: tuple[str, ...], workspace: Path) -> subprocess.CompletedProcess[str]:
    """The container's paths mapped onto this test's folders; `python` is this interpreter."""
    mapped = [a.replace(recipes.PYTEST_CONFIG, IMAGE_CONFIG.as_posix()).replace("/workspace", workspace.as_posix()) for a in argv]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return subprocess.run([sys.executable, *mapped[1:]], cwd=workspace, env=env, capture_output=True, text=True, timeout=120, check=False)


def test_the_component_imports_and_the_projects_own_settings_are_never_read(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run = _run(recipes.command_for(recipes.RECIPES["python-fastapi"], "backend"), workspace)
    counts = recipes.parse_pytest(run.stdout)
    assert run.returncode == 0, (run.stdout + run.stderr)[-2000:]
    assert counts.readable and (counts.ran, counts.failures, counts.passed_names) == (1, 0, ("test_ac_001_answer",))
    assert "outside conftest loaded" not in run.stdout + run.stderr
    assert not [p for p in workspace.rglob("*") if p.name in (".pytest_cache", "__pycache__")]  # nothing written into the project


def test_without_those_options_the_same_project_cannot_be_judged(tmp_path: Path) -> None:
    """The arguments the recipe had before: the project's settings and the outside conftest take over."""
    workspace = _workspace(tmp_path)
    run = _run(("python", "-I", "-B", "-m", "pytest", "-v", "-p", "no:cacheprovider", "--color=no", "/workspace/backend"), workspace)
    counts = recipes.parse_pytest(run.stdout)
    assert run.returncode != 0 and not (counts.readable and counts.ran == 1 and counts.failures == 0)
