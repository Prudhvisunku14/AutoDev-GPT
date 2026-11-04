"""The website verifier's self-test projects: two that must pass and two that must fail.

`devpilot setup --web` runs all four on the real image before it records the image as approved. A verifier that passes
a broken site, or fails a working one, is never approved. The React project pins exact versions of its direct
dependencies; it has no lockfile, so the install also proves the proxy path to the registry works.
"""

from __future__ import annotations

_STATIC_OK = {
    "index.html": """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fixture home</title><link rel="stylesheet" href="style.css"><link rel="icon" href="favicon.svg"></head>
<body><main><h1>Fixture home</h1><p id="status">Ready</p><button id="go" type="button">Say hello</button><a href="about.html">About</a></main>
<script src="app.js"></script></body></html>
""",
    "about.html": """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>About</title><link rel="stylesheet" href="style.css"></head>
<body><main><h1>About this fixture</h1><a href="index.html">Home</a></main></body></html>
""",
    "style.css": "body{font-family:sans-serif;margin:2rem}button{padding:.5rem 1rem}\n",
    "app.js": "document.getElementById('go').addEventListener('click',()=>{document.getElementById('status').textContent='Hello'});\n",
    "favicon.svg": '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="#246"/></svg>\n',
}

_REACT_OK = {
    "package.json": """{
  "name": "devpilot-web-fixture",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {"build": "tsc -b && vite build"},
  "dependencies": {"react": "18.3.1", "react-dom": "18.3.1"},
  "devDependencies": {"@types/react": "18.3.12", "@types/react-dom": "18.3.1", "@vitejs/plugin-react": "4.3.4", "typescript": "5.6.3", "vite": "5.4.11"}
}
""",
    "index.html": """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fixture app</title></head>
<body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>
""",
    "tsconfig.json": """{
  "compilerOptions": {"target": "ES2020", "lib": ["ES2020", "DOM"], "module": "ESNext", "moduleResolution": "bundler", "jsx": "react-jsx",
                      "strict": true, "noEmit": true, "skipLibCheck": true, "isolatedModules": true},
  "include": ["src"]
}
""",
    "vite.config.ts": "import { defineConfig } from 'vite';\nimport react from '@vitejs/plugin-react';\n\nexport default defineConfig({ plugins: [react()] });\n",
    "src/main.tsx": """import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
""",
    "src/App.tsx": """import { useState } from 'react';

export default function App() {
  const [count, setCount] = useState(0);
  return (
    <main>
      <h1>Fixture app</h1>
      <p>Clicked {count} times</p>
      <button type="button" onClick={() => setCount((c) => c + 1)}>Count</button>
    </main>
  );
}
""",
}

# Must FAIL: an uncaught error when the button is clicked, and an image that does not exist.
_STATIC_BROKEN = {
    "index.html": """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Broken</title></head>
<body><h1>Broken fixture</h1><img src="missing.png" alt=""><button type="button" onclick="notDefined()">Break</button></body></html>
""",
}

# Must FAIL at the type check: a TypeScript error the build alone would not catch.
_REACT_BROKEN = {**_REACT_OK, "src/App.tsx": "export default function App() {\n  const n: number = 'not a number';\n  return <main><h1>{n}</h1></main>;\n}\n"}

# (label, kind, files, must pass, the stage a broken one must fail at)
SELF_TEST: tuple[tuple[str, str, dict[str, str], bool, str | None], ...] = (
    ("static site", "static-html", _STATIC_OK, True, None),
    ("React + Vite + TypeScript app", "react-vite", _REACT_OK, True, None),
    ("static site with a missing image and a script error", "static-html", _STATIC_BROKEN, False, "assets"),
    ("React app with a type error", "react-vite", _REACT_BROKEN, False, "typecheck"),
)


def files(project: dict[str, str]) -> dict[str, bytes]:
    return {path: text.encode("utf-8") for path, text in project.items()}


# --- Independent browser acceptance -------------------------------------------------------------------------------
# A small site with real interactions, and the frozen checks a run would hold for it. The broken copy keeps everything but
# the form's validation, so exactly AC-003 must fail, at the acceptance stage, while the built-in checks still pass.

_INTERACTIVE_JS = """\
'use strict';
const projects = [{ name: 'Harbor app', year: 2025 }, { name: 'Field notes', year: 2024 }, { name: 'Open data portal', year: 2023 }];
const list = document.getElementById('project-list');
for (const p of projects) { const li = document.createElement('li'); li.textContent = `${p.name} (${p.year})`; list.appendChild(li); }
const toggle = document.getElementById('menu-toggle');
const menu = document.getElementById('site-menu');
toggle.addEventListener('click', () => { const open = menu.hidden; menu.hidden = !open; toggle.setAttribute('aria-expanded', String(open)); });
const form = document.getElementById('contact-form');
const status = document.getElementById('form-status');
form.addEventListener('submit', (event) => {
  event.preventDefault();
  const email = document.getElementById('email').value.trim();
  const message = document.getElementById('message').value.trim();
  status.textContent = (!email.includes('@') || !message) ? 'Please enter a valid email and a message.' : 'Thank you, your message was sent.';
});
"""

_INTERACTIVE = {
    "index.html": """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fixture Studio</title><link rel="stylesheet" href="style.css"></head>
<body>
<header><button id="menu-toggle" type="button" aria-expanded="false" aria-controls="site-menu">Menu</button>
<nav id="site-menu" aria-label="Main" hidden><a href="#work">Work</a> <a href="#contact">Contact</a></nav></header>
<main>
<h1>Fixture Studio</h1>
<section id="work"><h2>Selected work</h2><ul id="project-list"></ul></section>
<section id="contact"><h2>Contact</h2>
<form id="contact-form" novalidate>
<label for="email">Email</label><input id="email" name="email" type="email">
<label for="message">Message</label><textarea id="message" name="message"></textarea>
<button type="submit">Send message</button>
<p id="form-status" role="status"></p>
</form></section>
</main>
<script src="app.js"></script>
</body></html>
""",
    "style.css": "body{font-family:sans-serif;margin:2rem}nav a{margin-right:1rem}label{display:block;margin-top:.5rem}\n",
    "app.js": _INTERACTIVE_JS,
}

_INTERACTIVE_BROKEN = {**_INTERACTIVE, "app.js": _INTERACTIVE_JS.replace(
    "status.textContent = (!email.includes('@') || !message) ? 'Please enter a valid email and a message.' : 'Thank you, your message was sent.';",
    "status.textContent = 'Thank you, your message was sent.';",
)}


def _step(action: str, *, by: str | None = None, role: str | None = None, target: str | None = None, value: str | None = None, count: int | None = None) -> dict[str, object]:
    return {"action": action, "by": by, "role": role, "target": target, "value": value, "count": count}


ACCEPTANCE_CHECKS: tuple[dict[str, object], ...] = (
    {"code": "AC-001", "priority": "MUST", "start": "/", "steps": [
        _step("expect_hidden", by="role", role="link", target="Contact"), _step("click", by="role", role="button", target="Menu"),
        _step("expect_visible", by="role", role="link", target="Contact"), _step("click", by="role", role="link", target="Contact"),
        _step("expect_url", value="/#contact")]},
    {"code": "AC-002", "priority": "MUST", "start": "/", "steps": [
        _step("expect_visible", by="role", role="heading", target="Selected work"), _step("expect_count", by="css", target="#project-list li", count=3)]},
    {"code": "AC-003", "priority": "MUST", "start": "/", "steps": [
        _step("click", by="role", role="button", target="Send message"), _step("expect_text", value="Please enter a valid email"),
        _step("fill", by="label", target="Email", value="ada@example.org"), _step("fill", by="label", target="Message", value="Hello there"),
        _step("click", by="role", role="button", target="Send message"), _step("expect_text", value="Thank you")]},
    {"code": "AC-004", "priority": "SHOULD", "start": "/", "steps": [_step("expect_title", value="Fixture Studio")]},
)  # fmt: skip

# (label, files, must pass, the check codes that must fail)
ACCEPTANCE_SELF_TEST: tuple[tuple[str, dict[str, str], bool, tuple[str, ...]], ...] = (
    ("interactive site with frozen acceptance checks", _INTERACTIVE, True, ()),
    ("the same site with its form validation removed", _INTERACTIVE_BROKEN, False, ("AC-003",)),
)
