"""The React runner's copy and its checks (backend/sandbox_images/verify-react/run-vitest.mjs), run with the host's Node over
folders this test writes. **Vitest never runs here and the image does not exist: this is not a Docker test.** Skipped
where Node is not installed."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

IMAGE = Path(__file__).resolve().parents[2] / "sandbox_images" / "verify-react"
RUNNER = IMAGE / "run-vitest.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")

COMPONENT = {
    "package.json": '{"dependencies": {"react": "18"}}',
    "index.html": "<div id=root></div>\n",
    "src/App.jsx": "export const App = () => null;\n",
    "src/App.test.jsx": "test('AC-001 renders', () => {});\n",
    "src/styles.css": "body {}\n",
    "src/logo.png": "not an approved file type",
    ".env": "VITE_SECRET=x\n",
    "node_modules/left-pad/index.js": "module.exports = 1;\n",  # the image's own node_modules is linked instead
    "dist/bundle.js": "built\n",
}


def _node(body: str, cwd: Path) -> Any:
    """Runs `body` with the runner imported as `r`. The body prints one JSON value."""
    code = f"import * as r from {json.dumps(RUNNER.as_uri())};\n{body}"
    run = subprocess.run([str(NODE), "--input-type=module", "-e", code], cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)


def _prepare(source: Path, dest: Path, limits: dict[str, int] | None = None) -> dict[str, Any]:
    extra = f", {{...r.LIMITS, ...{json.dumps(limits)}}}" if limits else ""
    return _node(
        f"try {{ console.log(JSON.stringify({{ok: r.prepare({json.dumps(str(source))}, {json.dumps(str(dest))}{extra})}})); }}\n"
        "catch (e) { console.log(JSON.stringify({refused: e instanceof r.Refused, message: e.message})); }",
        source.parent,
    )


def _component(tmp_path: Path) -> Path:
    root = tmp_path / "frontend"
    for rel, text in COMPONENT.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


def _link(target: Path, link: Path) -> None:
    """A folder link: an NTFS junction on Windows (no admin rights needed), a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_only_the_components_approved_files_are_copied(tmp_path: Path) -> None:
    source = _component(tmp_path)
    before = _tree(source)
    result = _prepare(source, tmp_path / "app")["ok"]
    assert result["copied"] == ["index.html", "package.json", "src/App.jsx", "src/App.test.jsx", "src/styles.css"]
    assert result["skipped"] == [".env", "dist/", "node_modules/", "src/logo.png"]
    assert _tree(tmp_path / "app") == {rel: before[rel] for rel in result["copied"]} and _tree(source) == before  # the source is only read


def test_a_link_anywhere_stops_the_copy(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.js").write_text("export default 'host file';\n", encoding="utf-8")
    source = _component(tmp_path)
    _link(outside, source / "src" / "escape")
    refused = _prepare(source, tmp_path / "app")
    assert refused == {"refused": True, "message": "src/escape is a link; links are never followed"}
    _link(source, tmp_path / "linked")
    assert _prepare(tmp_path / "linked", tmp_path / "app2") == {"refused": True, "message": "the component folder is a link"}


def test_a_hard_linked_file_stops_the_copy(tmp_path: Path) -> None:
    source = _component(tmp_path)
    os.link(source / "src" / "App.jsx", tmp_path / "elsewhere.jsx")
    assert _prepare(source, tmp_path / "app") == {"refused": True, "message": "src/App.jsx has other names (a hard link)"}


@pytest.mark.parametrize(
    ("limits", "message"),
    [({"files": 2}, "over 2 files"), ({"bytes": 40}, "over 2000 files or 40 bytes"), ({"fileBytes": 10}, "is over 10 bytes"), ({"depth": 0}, "nested more than 0 folders")],
)
def test_the_limits_stop_the_copy_before_vitest_could_start(tmp_path: Path, limits: dict[str, int], message: str) -> None:
    refused = _prepare(_component(tmp_path), tmp_path / "app", limits)
    assert refused["refused"] and message in refused["message"]


def test_an_existing_destination_is_refused(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    assert _prepare(_component(tmp_path), tmp_path / "app")["message"].endswith("already exists")


def test_only_a_one_level_workspace_component_is_accepted(tmp_path: Path) -> None:
    args = ["/workspace/frontend", "/workspace/web-app_2", "/workspace/../etc", "/workspace/frontend/src", "/workspace/Frontend", "/etc/passwd", "/workspace/frontend/",
            "/workspace/", "workspace/frontend", "/workspace/frontend\n", "/workspace/frontend/../../etc"]  # fmt: skip
    found = _node(
        f"const out = {{}}; for (const a of {json.dumps(args)}) {{ try {{ out[a] = r.componentDir(a) === a; }} catch (e) {{ out[a] = e instanceof r.Refused ? 'refused' : e.message; }} }}\n"
        "console.log(JSON.stringify(out));",
        tmp_path,
    )
    assert found == {a: (True if a in ("/workspace/frontend", "/workspace/web-app_2") else "refused") for a in args}


def test_vitest_always_gets_devpilots_config_and_keeps_its_cache_in_tmp(tmp_path: Path) -> None:
    found = _node("console.log(JSON.stringify({args: r.vitestArgs(), test: r.childEnv(false, {PATH: '/bin', OPENAI_API_KEY: 'x'}), acceptance: r.childEnv(true, {})}));", tmp_path)
    args = found["args"]
    assert args[:2] == ["/opt/verify/node_modules/vitest/vitest.mjs", "run"] and args[args.index("--config") + 1] == "/opt/verify/vitest.config.mjs"
    assert args[args.index("--root") + 1] == "/tmp/app" and "--reporter=json" in args and "--outputFile=/tmp/vitest-report.json" in args
    assert found["test"] == {"PATH": "/bin", "HOME": "/tmp", "TMPDIR": "/tmp", "CI": "true", "NO_COLOR": "1", "DEVPILOT_VITEST_MODE": "component"}  # nothing else passes
    assert found["acceptance"]["DEVPILOT_VITEST_MODE"] == "acceptance"
    config = (IMAGE / "vitest.config.mjs").read_text(encoding="utf-8")
    assert 'cacheDir: "/tmp/vite-cache"' in config and "allowOnly: false" in config and 'environment: "jsdom"' in config and '"__acceptance__/**/*.test.{js,jsx,ts,tsx}"' in config


def test_the_runner_can_reach_nothing_but_vitest() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    code = re.sub(r"//.*$", "", source, flags=re.MULTILINE)
    assert set(re.findall(r'from "([^"]+)"', code)) == {"node:child_process", "node:fs", "node:path", "node:url"}  # no network module
    assert code.count("spawnSync(") == 1 and "spawnSync(process.execPath, vitestArgs()" in code  # one process: Vitest
    assert not re.search(r"\b(npm|npx|fetch|https?|install|exec|execSync|spawn)\b", code)


def test_bad_arguments_stop_the_runner_before_anything_runs(tmp_path: Path) -> None:
    for args in (["../etc"], ["/workspace/frontend", "--watch"], []):
        run = subprocess.run([str(NODE), str(RUNNER), *args], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)
        assert run.returncode == 3 and run.stdout == "" and "refused" in run.stderr
