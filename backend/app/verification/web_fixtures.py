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
