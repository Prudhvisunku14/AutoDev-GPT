"""What an existing project is, read from its files alone: framework, stack, pages, components, assets and README.

Nothing is run and no script is followed. Manifests are parsed as data (package.json as JSON; requirements and
pyproject as text), and the README is untrusted text shown to the person, never an instruction. The result names one
of DevPilot's stacks when the project matches one exactly; otherwise it names the framework it found and says the
project is not supported, rather than forcing it into a stack it is not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# Front-end frameworks DevPilot recognises but cannot build: named, never treated as React + Vite or static HTML.
_UNSUPPORTED_JS = {
    "next": "Next.js", "nuxt": "Nuxt", "@angular/core": "Angular", "vue": "Vue", "svelte": "Svelte", "@sveltejs/kit": "SvelteKit", "astro": "Astro",
    "gatsby": "Gatsby", "@remix-run/react": "Remix", "react-scripts": "Create React App", "solid-js": "SolidJS", "preact": "Preact", "@docusaurus/core": "Docusaurus",
    "react-native": "React Native", "expo": "Expo", "electron": "Electron",
}  # fmt: skip
_UNSUPPORTED_PY = {"django": "Django", "flask": "Flask", "streamlit": "Streamlit"}
_ASSET = re.compile(r"\.(png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|otf|eot|mp4|webm|mp3|wav|pdf)$", re.IGNORECASE)
_SKIP = ("node_modules/", "dist/", "build/", ".next/", "coverage/", "vendor/", "__pycache__/")
MAX_COMPONENT_DEPTH = 1  # a component lives at the root or in one folder (frontend/, backend/, web/ …), as the plan gate allows


@dataclass(frozen=True)
class Component:
    path: str  # "" for the repository root
    kind: str  # react-vite | static-html | fastapi | python | node-other | unsupported
    framework: str  # what was found, in words
    typescript: bool = False
    package_manager: str | None = None  # npm | yarn | pnpm | bun, from the lockfile
    scripts: tuple[str, ...] = ()  # package.json script names: reported, never run
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectProfile:
    stack: str | None  # a DevPilot stack id when the project is exactly one, else None
    summary: str
    components: tuple[Component, ...]
    pages: tuple[str, ...]
    routes: tuple[str, ...]  # client-side routes found in the source, e.g. "/about"
    ui_components: tuple[str, ...]
    assets: tuple[str, ...]
    readme_excerpt: str  # untrusted
    unsupported: tuple[str, ...]  # frameworks found that DevPilot cannot build
    notes: tuple[str, ...] = field(default_factory=tuple)
    file_count: int = 0

    def describe(self) -> str:
        lines = [self.summary, f"Files: {self.file_count}"]
        for c in self.components:
            where = c.path or "(root)"
            extra = ", ".join(x for x in (("TypeScript" if c.typescript else ""), (f"{c.package_manager} lockfile" if c.package_manager else ""), (f"scripts: {', '.join(c.scripts[:6])}" if c.scripts else "")) if x)
            lines.append(f"  {where}: {c.framework}" + (f" ({extra})" if extra else ""))
        for title, items in (("Pages", self.pages), ("Routes", self.routes), ("Components", self.ui_components), ("Assets", self.assets)):
            if items:
                shown = ", ".join(items[:12]) + (f" … and {len(items) - 12} more" if len(items) > 12 else "")
                lines.append(f"{title} ({len(items)}): {shown}")
        lines += [f"Note: {n}" for n in self.notes]
        return "\n".join(lines)


def _visible(path: str) -> bool:
    return not any(f"/{s}" in f"/{path}" for s in _SKIP)


def _folder(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _depth(folder: str) -> int:
    return 0 if not folder else folder.count("/") + 1


def _lockfile(names: set[str], folder: str) -> str | None:
    prefix = f"{folder}/" if folder else ""
    for name, manager in (("package-lock.json", "npm"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun")):
        if prefix + name in names:
            return manager
    return None


def _js_component(folder: str, manifest: Mapping[str, object], names: set[str]) -> Component:
    deps = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}  # type: ignore[dict-item]
    dep_names = tuple(sorted(str(d) for d in deps))
    scripts = tuple(sorted(str(s) for s in (manifest.get("scripts") or {})))  # type: ignore[union-attr]
    prefix = f"{folder}/" if folder else ""
    ts = "typescript" in deps or f"{prefix}tsconfig.json" in names or any(n.startswith(prefix) and n.endswith((".ts", ".tsx")) and _visible(n) for n in names)
    manager = _lockfile(names, folder)
    for dep, label in _UNSUPPORTED_JS.items():
        if dep in deps:
            return Component(folder, "unsupported", label, ts, manager, scripts, dep_names)
    if "react" in deps and "vite" in deps:
        return Component(folder, "react-vite", "React + Vite" + (" + TypeScript" if ts else ""), ts, manager, scripts, dep_names)
    if "react" in deps:
        return Component(folder, "unsupported", "React without Vite", ts, manager, scripts, dep_names)
    if f"{prefix}index.html" in names and not ({"build", "dev"} & set(scripts)):
        return Component(folder, "static-html", "Static HTML (with a package.json for tooling only)", ts, manager, scripts, dep_names)
    return Component(folder, "node-other", "Node.js project (no recognised front-end framework)", ts, manager, scripts, dep_names)


def _py_component(folder: str, text: str) -> Component:
    lowered = text.lower()
    names = set(re.findall(r"(?m)^\s*[\"']?([a-z0-9_.-]+)", lowered)) | set(re.findall(r"[\"']([a-z0-9_.-]+)\s*(?:[<>=!~\[;]|[\"'])", lowered))
    for dep, label in _UNSUPPORTED_PY.items():
        if dep in names:
            return Component(folder, "unsupported", label)
    if "fastapi" in names:
        return Component(folder, "fastapi", "FastAPI")
    return Component(folder, "python", "Python")


def inspect_project(files: Mapping[str, int], read: Callable[[str], str | None], *, truncated: bool = False) -> ProjectProfile:
    """Profile a project from its file list (path -> size) and a reader for small text files. `read` may return None."""
    names = {p for p in files if _visible(p)}
    components: list[Component] = []
    notes: list[str] = []
    if truncated:
        notes.append("GitHub cut the file list short; the profile may be incomplete")

    for manifest_path in sorted(p for p in names if PurePosixPath(p).name == "package.json" and _depth(_folder(p)) <= MAX_COMPONENT_DEPTH):
        text = read(manifest_path)
        try:
            manifest = json.loads(text) if text else None
        except ValueError:
            manifest = None
        if not isinstance(manifest, dict):
            notes.append(f"{manifest_path} could not be read as JSON")
            continue
        components.append(_js_component(_folder(manifest_path), manifest, names))

    js_folders = {c.path for c in components}
    py_folders: dict[str, str] = {}
    for manifest_path in sorted(p for p in names if PurePosixPath(p).name in ("requirements.txt", "pyproject.toml") and _depth(_folder(p)) <= MAX_COMPONENT_DEPTH):
        py_folders.setdefault(_folder(manifest_path), "")
        py_folders[_folder(manifest_path)] += (read(manifest_path) or "") + "\n"
    for folder, text in sorted(py_folders.items()):
        components.append(_py_component(folder, text))
    html = sorted(p for p in names if p.lower().endswith((".html", ".htm")))
    has_site_entry = any(
        _depth(_folder(path)) <= MAX_COMPONENT_DEPTH and PurePosixPath(path).name.lower() == "index.html"
        for path in html
    )
    if not py_folders and any(p.endswith(".py") for p in names) and not components and not has_site_entry:
        components.append(Component("", "python", "Python"))

    if not components and html:
        roots = [p for p in html if _depth(_folder(p)) <= MAX_COMPONENT_DEPTH and PurePosixPath(p).name.lower() == "index.html"]
        folder = _folder(roots[0]) if roots else ""
        if folder not in js_folders:
            components.append(Component(folder, "static-html", "Static HTML/CSS/JavaScript"))
            if not roots:
                notes.append("no index.html was found at the root or one folder down")

    kinds = sorted({c.kind for c in components})
    unsupported = tuple(sorted({c.framework for c in components if c.kind == "unsupported"}))
    stack: str | None = None
    if unsupported:
        summary = f"Uses {', '.join(unsupported)}, which DevPilot cannot build"
    elif kinds == ["react-vite"] and len(components) == 1:
        stack, summary = "react-vite", components[0].framework + " single-page application"
    elif kinds == ["static-html"] and len(components) == 1:
        stack, summary = "static-html", "Static website (HTML, CSS and JavaScript)"
    elif set(kinds) == {"react-vite", "fastapi"} and all(c.path for c in components):
        stack, summary = "react-fastapi", "React + Vite front end with a FastAPI back end"
    elif kinds == ["fastapi"] and len(components) == 1:
        stack, summary = "python-fastapi", "FastAPI service"
    elif kinds == ["python"] and len(components) == 1:
        stack, summary = "python-cli", "Python project"
    elif not components:
        summary = "No recognised project type (no package.json, Python manifest or HTML page)"
    else:
        summary = "Mixed project: " + ", ".join(f"{c.path or '(root)'} is {c.framework}" for c in components)
    for c in components:
        if c.kind == "react-vite" and c.package_manager not in (None, "npm"):
            notes.append(f"{c.path or '(root)'} uses {c.package_manager}; DevPilot installs with npm only, from package.json (its {c.package_manager} lockfile is not used)")
        if c.kind == "react-vite" and c.package_manager is None:
            notes.append(f"{c.path or '(root)'} has no package-lock.json; versions are resolved from package.json ranges at install time")

    sources = sorted(p for p in names if p.endswith((".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte")))
    pages = tuple(html + [p for p in sources if re.search(r"(^|/)(pages|views|routes|app)/", p)])
    ui = tuple(p for p in sources if re.search(r"(^|/)components/", p))
    routes = _routes(sources, read)
    assets = tuple(sorted(p for p in names if _ASSET.search(p)))
    readme_path = next((p for p in sorted(names, key=lambda n: (n.count("/"), n)) if PurePosixPath(p).name.lower() in ("readme.md", "readme", "readme.txt", "readme.rst")), None)
    readme = (read(readme_path) or "") if readme_path else ""
    return ProjectProfile(stack, summary, tuple(components), pages, routes, ui, assets, _excerpt(readme), unsupported, tuple(notes), len(names))


_ROUTE = re.compile(r"""(?:path\s*[=:]\s*|<Route[^>]*?\bpath=)\s*\{?\s*["'`](/[A-Za-z0-9_\-/:.*]*)["'`]""")


def _routes(sources: list[str], read: Callable[[str], str | None]) -> tuple[str, ...]:
    found: set[str] = set()
    for path in [p for p in sources if re.search(r"(App|main|router|routes?)\.(t|j)sx?$", p, re.IGNORECASE)][:12]:
        text = read(path) or ""
        found.update(_ROUTE.findall(text[:200_000]))
    return tuple(sorted(found))


def _excerpt(readme: str, limit: int = 1_200) -> str:
    text = re.sub(r"<[^>]{0,200}>", "", readme)  # tags and comments, which can hide text from a reader
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"
