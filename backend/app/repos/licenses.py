"""Which licences allow reuse, and what each one requires. Decided in code, never by a model.

Only permissive licences are reused by default. A copyleft licence would bind the user's delivered project to its
terms, so it is refused until a person decides otherwise. No licence, or one the host cannot identify, means no
permission to reuse at all.
"""

from __future__ import annotations

from dataclasses import dataclass

PERMISSIVE = {
    "MIT": "keep the LICENSE file and its copyright notice",
    "Apache-2.0": "keep the LICENSE file and any NOTICE file, and mark changed files",
    "BSD-2-Clause": "keep the LICENSE file and its copyright notice",
    "BSD-3-Clause": "keep the LICENSE file and its copyright notice; do not use the authors' names to endorse",
    "ISC": "keep the LICENSE file and its copyright notice",
    "0BSD": "no condition; the LICENSE file is kept as a record",
    "Unlicense": "no condition; the LICENSE file is kept as a record",
    "Zlib": "keep the LICENSE file; mark changed files as altered",
}
_COPYLEFT_PREFIXES = ("GPL-", "AGPL-", "LGPL-", "MPL-", "EUPL-", "CC-BY-SA-", "OSL-", "EPL-")
_UNKNOWN = {None, "", "NOASSERTION", "OTHER", "other"}


@dataclass(frozen=True)
class LicenseVerdict:
    spdx: str | None
    allowed: bool
    reason: str
    attribution: str = ""


def judge_license(spdx: str | None, license_text: str) -> LicenseVerdict:
    if spdx in _UNKNOWN:
        return LicenseVerdict(spdx, False, "no licence the host can identify: without one there is no permission to reuse the code")
    assert spdx is not None
    if spdx.startswith(_COPYLEFT_PREFIXES):
        return LicenseVerdict(spdx, False, f"{spdx} is copyleft: the delivered project would have to adopt its terms, so it is not reused without a person's decision")
    if spdx not in PERMISSIVE:
        return LicenseVerdict(spdx, False, f"{spdx} is not on the approved list of licences")
    if not license_text.strip():
        return LicenseVerdict(spdx, False, f"{spdx} is reported, but the licence file is missing, so attribution could not be kept")
    return LicenseVerdict(spdx, True, f"{spdx} allows reuse", PERMISSIVE[spdx])
