"""What a build request asks for, and whether DevPilot can build it now: capability-based dispatch, not a keyword ban.

`classify` reads the request: the GitHub repository it names (if any), the kind of application (website, full-stack,
command-line), the intent when a repository is given (adapt it, modify it, or rebuild it), and a stack hint. When a
repository is given, its own files decide the stack (`app.repos.inspect`); the words only describe the change.

`decide` asks the same readiness check the engine uses (`verification.recipes.readiness`) whether that stack can be
built *and verified* now. Nothing is accepted on words alone, and nothing is built as something it is not: a website is
never built as a Python program, and a framework DevPilot cannot verify is named and declined with the exact reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.repos.github_url import RepoRef, find_repo_url
from app.repos.inspect import ProjectProfile
from app.verification.recipes import readiness

STACK_NAMES = {
    "python-cli": "Python command-line application", "static-html": "static website (HTML, CSS, JavaScript)", "react-vite": "React + Vite + TypeScript website",
    "react-fastapi": "React + FastAPI full-stack application", "python-fastapi": "FastAPI service",
}  # fmt: skip

_W = r"(?<![\w-])"  # a word start that a hyphen does not count as
_MOBILE = re.compile(_W + r"(mobile app|android|ios app|iphone app|swift|kotlin|flutter|react native|expo app)\b", re.IGNORECASE)
_OTHER_FRAMEWORKS = re.compile(_W + r"(vue(?:\.js)?|angular|svelte(?:kit)?|next\.?js|nuxt|django|flask|express(?:\.js)?|laravel|rails|ruby on rails|spring boot|asp\.net|php|wordpress|gatsby|astro|remix)\b", re.IGNORECASE)
_FULLSTACK = re.compile(_W + r"(fastapi|full[- ]?stack|back[- ]?end|rest api|api server|database|login|sign[- ]?up|authentication|rag|retrieval[- ]augmented|vector (?:store|search|database)|chatbot with documents)\b", re.IGNORECASE)
_REACT = re.compile(_W + r"(react|vite|typescript|tsx|single[- ]page|spa|dashboard|web app(?:lication)?|interactive)\b", re.IGNORECASE)
_STATIC = re.compile(_W + r"(static|plain html|html(?:/| and | )css|landing page|homepage|home page|portfolio|brochure|one[- ]page)\b", re.IGNORECASE)
_WEBSITE = re.compile(_W + r"(website|web ?site|web ?page|webpage|homepage|home page|landing page|pages?|front[- ]?end|site|html|css|dashboard|portfolio|web app(?:lication)?|react|vite)\b", re.IGNORECASE)
_CLI = re.compile(_W + r"(command[- ]line|cli|terminal|console app|python (?:tool|script|program)|script)\b", re.IGNORECASE)
_REBUILD = re.compile(_W + r"(rebuild|re-build|recreate|re-create|redesign|rewrite|from scratch|start over|clone)\b", re.IGNORECASE)
_MODIFY = re.compile(_W + r"(modify|update|change|fix|add|extend|improve|edit|refactor|upgrade|enhance|replace|remove)\b", re.IGNORECASE)


@dataclass(frozen=True)
class BuildRequest:
    text: str
    repo: RepoRef | None
    kind: str  # website | fullstack | cli | mobile | unknown
    intent: str  # new | adapt | modify | rebuild
    stack_hint: str | None
    unsupported: str | None  # a framework or platform named in the words that DevPilot cannot build


def classify(text: str) -> BuildRequest:
    """Raises RepoUrlError when the request names a repository DevPilot will not fetch (wrong host, credentials in it…)."""
    repo = find_repo_url(text)
    words = re.sub(r"\S*github\.com\S*", " ", text)  # the URL's own words (owner, name) say nothing about the change
    mobile = _MOBILE.search(words)
    other = _OTHER_FRAMEWORKS.search(words)
    unsupported = mobile.group(1) if mobile else other.group(1) if other else None
    if mobile:
        kind, hint = "mobile", None
    elif _FULLSTACK.search(words) and (_WEBSITE.search(words) or _REACT.search(words)):
        kind, hint = "fullstack", "react-fastapi"
    elif _WEBSITE.search(words) or _REACT.search(words):
        kind = "website"
        hint = "static-html" if _STATIC.search(words) and not _REACT.search(words) else "react-vite"
    elif _CLI.search(words):
        kind, hint = "cli", "python-cli"
    elif _FULLSTACK.search(words):
        kind, hint = "fullstack", "python-fastapi"
    else:
        kind, hint = "unknown", None
    if repo is None:
        intent = "new"
    elif _REBUILD.search(words):
        intent = "rebuild"
    elif _MODIFY.search(words):
        intent = "modify"
    else:
        intent = "adapt"
    return BuildRequest(text, repo, kind, intent, hint, unsupported)


@dataclass(frozen=True)
class Decision:
    stack: str | None
    accepted: bool
    reason: str  # why it can or cannot be built, in words a person can act on

    @property
    def stack_name(self) -> str:
        return STACK_NAMES.get(self.stack or "", self.stack or "unknown")


def decide(request: BuildRequest, profile: ProjectProfile | None = None, *, chosen_stack: str | None = None) -> Decision:
    """Whether to build, on which stack. A repository's own files outrank the words; the words outrank the default."""
    if request.unsupported and not (profile and profile.stack):
        return Decision(None, False, f"“{request.unsupported}” cannot be built: DevPilot has no way to build or verify it (it supports Python command-line applications, static HTML and React + Vite, each only once its verification is ready)")
    if profile is not None:
        if profile.unsupported:
            return Decision(None, False, f"the repository uses {', '.join(profile.unsupported)}, which DevPilot cannot build or verify")
        if profile.stack is None:
            return Decision(None, False, f"the repository is not a project type DevPilot can build: {profile.summary}")
        stack = profile.stack
        if request.kind == "cli" and stack != "python-cli":
            return Decision(stack, False, f"the request asks for a command-line program, but the repository is a {STACK_NAMES.get(stack, stack)}")
        if request.kind in ("website", "fullstack") and stack == "python-cli":
            return Decision(stack, False, "the request asks for a website, but the repository is a Python program, not a website")
    elif chosen_stack is not None:
        stack = chosen_stack
    elif request.kind == "unknown":
        stack = "python-cli"  # the one stack verified end to end; the plan gate still checks the requirements fit it
    elif request.stack_hint is None:
        return Decision(None, False, "DevPilot cannot build mobile applications")
    else:
        stack = request.stack_hint
    if chosen_stack is not None and chosen_stack != stack:
        return Decision(stack, False, f"--stack {chosen_stack} does not match the repository, which is a {STACK_NAMES.get(stack, stack)}")
    ok, why = readiness(stack)
    if not ok:
        hint = ". An existing site of this kind can be checked now with `devpilot verify-site`" if stack in ("static-html", "react-vite") and "verify-site" not in why else ""
        return Decision(stack, False, f"a {STACK_NAMES.get(stack, stack)} cannot be built yet: {why}{hint}")
    return Decision(stack, True, why)
