"""Opt-in real Chromium checks of deterministic fixtures; no model or credentials.

Only the fixed static self-test fixtures bypass image approval, exactly as setup's
self-test does. This does not grant approval or enable website generation.
"""
import pytest

from app.core.egress import EgressPolicy
from app.verification.web import WebVerifier
from app.verification.web_fixtures import SELF_TEST, files


@pytest.mark.docker
@pytest.mark.parametrize("label,kind,project,must_pass,failing_stage", [f for f in SELF_TEST if f[1] == "static-html"])
def test_static_fixtures_in_real_chromium(tmp_path, label, kind, project, must_pass, failing_stage):
    verifier = WebVerifier(EgressPolicy({}), work_dir=tmp_path, timeout=90)
    result = verifier.verify(files(project), kind, require_approval=False)
    assert result.cleanup == "removed", result.describe()
    if must_pass:
        assert result.passed and result.pages, result.describe()
        assert any(s.name == "browser" and s.ok and not s.skipped for s in result.stages)
    else:
        assert result.verdict == "FAILED" and result.reason.startswith(f"{failing_stage}:"), result.describe()
