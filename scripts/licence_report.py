#!/usr/bin/env python3
"""Licence report and gate (spec section 17). Prints CSV of what's installed and exits 1 on anything not allowed.

Run it inside the image being shipped, so it reports exactly what ships:
  docker run --rm --entrypoint python doclib:0.1.0 - < scripts/licence_report.py
"""

from __future__ import annotations

import csv
import re
import sys
from importlib import metadata

ALLOWED = ("mit", "apache", "bsd", "isc", "postgresql", "mpl", "mozilla public")
# Permissive, but not named in spec section 17, so they are reported for review rather than failing the build.
REVIEW = ("python software foundation", "psf", "unlicense", "hpnd", "mit-cmu", "cc0", "bsl-1.0", "boost", "zlib", "0bsd",
          "cc-by-4.0", "pil", "libpng", "cnri-python", "ncsa", "bzip2", "ofl", "ijg", "curl", "x11", "wtfpl", "python-2.0", "llvm-exception")
# BSL-1.0 is the Boost licence; only the Business Source Licence (BUSL or BSL-1.1) is banned.
FORBIDDEN = re.compile(r"\b(a?gpl|lgpl|sspl|busl|bsl-1\.1|business source|elastic|commons clause|non-?commercial|openrail)\b", re.IGNORECASE)
# Verified by hand: these packages declare their licence in a way this script can't read.
KNOWN = {"pypdfium2": "Apache-2.0 OR BSD-3-Clause"}
OWN_PACKAGES = {"doclib"}  # The product itself, not a dependency.


def licence_of(dist: metadata.Distribution) -> str:
    name = dist.metadata["Name"].lower()
    if name in KNOWN:
        return KNOWN[name]
    expression = dist.metadata.get("License-Expression")
    if expression:
        return expression
    classifiers = [c.split("::")[-1].strip() for c in dist.metadata.get_all("Classifier", []) if c.startswith("License ::")]
    specific = [c for c in classifiers if c != "OSI Approved"]
    if specific:
        return " OR ".join(specific)
    return (dist.metadata.get("License") or "UNKNOWN").strip().split("\n")[0][:80]


def _term(term: str) -> str:
    if FORBIDDEN.search(term):
        return "BLOCKED"
    if any(name in term for name in REVIEW):
        return "review"
    return "ok" if any(name in term for name in ALLOWED) else "UNKNOWN"


def verdict(licence: str) -> str:
    """SPDX logic: AND means every part must be acceptable. OR means the best option applies."""
    rank = {"ok": 0, "review": 1, "UNKNOWN": 2, "BLOCKED": 3}
    clean = re.sub(r"[()]", " ", licence.lower())
    results = []
    for option in re.split(r"\s+or\s+", clean):
        parts = [part.split(" with ")[0] for part in re.split(r"\s+and\s+", option)]
        results.append(max((_term(part.strip()) for part in parts), key=rank.get))
    return min(results, key=rank.get)


def main() -> int:
    writer = csv.writer(sys.stdout)
    writer.writerow(["package", "version", "licence", "verdict"])
    problems = 0
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        if dist.metadata["Name"].lower() in OWN_PACKAGES:
            writer.writerow([dist.metadata["Name"], dist.version, "this project", "self"])
            continue
        licence = licence_of(dist)
        result = verdict(licence)
        writer.writerow([dist.metadata["Name"], dist.version, licence, result])
        problems += result in {"BLOCKED", "UNKNOWN"}
    print(f"# {problems} package(s) need attention", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
