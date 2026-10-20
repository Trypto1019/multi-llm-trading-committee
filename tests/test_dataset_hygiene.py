"""Dataset hygiene: fail on real-looking PII or any private/brand identifier.

Every tracked text file is scanned for:
- phone numbers outside the reserved fictional range (555-01xx / +1555010xxxx)
- email addresses outside example.com
- IPv4 literals other than loopback/unspecified
- broker-style account ids
- terms on the private scrub list

The private scrub list lives outside the repo; set PORTFOLIO_SCRUB_TERMS to its
path to enable that check.
Only LICENSE may carry the copyright holder's name.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "out", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".toml", ".yml", ".yaml", ".txt", ".cfg", ".ini", ""}

def _load_private_terms() -> tuple[list[str], list[str], list[str]]:
    """Read private scrub terms from the file named by PORTFOLIO_SCRUB_TERMS.

    The terms live outside the repository so this public file never lists what
    it is meant to keep out. Without the variable, only the generic checks run.
    Line format: plain = forbidden term, "owner:" = name allowed only in
    README/LICENSE, "re:" = regex, "#" = comment.
    """
    path = os.environ.get("PORTFOLIO_SCRUB_TERMS", "")
    if not path or not Path(path).is_file():
        return [], [], []
    terms, owner, regexes = [], [], []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("owner:"):
            owner.append(line[len("owner:"):])
        elif line.startswith("re:"):
            regexes.append(line[len("re:"):])
        else:
            terms.append(line)
    return terms, owner, regexes


_TERMS, _OWNER, _REGEXES = _load_private_terms()
SCRUB_TERMS = [t.lower() for t in _TERMS + _OWNER]
# The copyright line in LICENSE is the one allowed use of the owner's name.
LICENSE_ONLY = {t.lower() for t in _OWNER}

PHONE_RE = re.compile(r"(?<![\d.])(?:\+?1[-. ]?)?\(?(\d{3})\)?[-. ](\d{3})[-. ](\d{4})(?!\d)")
E164_RE = re.compile(r"\+1\d{10}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
ACCOUNT_RE = re.compile(r"\bDU\d{5,}\b")
SHORT_NAME_RES = [re.compile(rx, re.I) for rx in _REGEXES]  # word-boundary short names

ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}


def tracked_files() -> list[Path]:
    files = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix in TEXT_SUFFIXES or p.name.startswith("."):
            files.append(p)
    return files


FILES = tracked_files()


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def test_scanner_sees_the_repo():
    names = {_rel(p) for p in FILES}
    assert "README.md" in names and "risk_marshal/marshal.py" in names


@pytest.mark.parametrize("path", FILES, ids=_rel)
def test_file_is_clean(path: Path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    lower = text.lower()
    problems: list[str] = []

    for term in SCRUB_TERMS:
        if term in LICENSE_ONLY and path.name == "LICENSE":
            continue
        if term in lower:
            problems.append(f"scrub term #{SCRUB_TERMS.index(term)}")
    if any(rx.search(text) for rx in SHORT_NAME_RES):
        problems.append("short-name scrub term")

    for m in PHONE_RE.finditer(text):
        if not (m.group(2) == "555" and m.group(3).startswith("01")):
            problems.append(f"phone-like number {m.group(0)!r}")
    for m in E164_RE.finditer(text):
        if not m.group(0).startswith("+1555010"):
            problems.append(f"E.164 number {m.group(0)!r}")
    for m in EMAIL_RE.finditer(text):
        if m.group(1).lower() != "example.com":
            problems.append(f"email domain {m.group(1)!r}")
    for m in IPV4_RE.finditer(text):
        if m.group(1) not in ALLOWED_IPS:
            problems.append(f"IPv4 literal {m.group(1)!r}")
    if ACCOUNT_RE.search(text):
        problems.append("broker-style account id")

    assert not problems, f"{_rel(path)}: {problems}"


def test_detectors_work():
    # Probe strings are assembled at runtime so this file stays clean under its own scan.
    real_phone = "-".join(["212", "555", "2368"])  # 555 exchange but outside the 01xx block
    fake_phone = "-".join(["202", "555", "0142"])
    ip = ".".join(["10", "1", "2", "3"])
    m = PHONE_RE.search("call " + real_phone)
    assert m and m.group(2) == "555" and not m.group(3).startswith("01")  # would be flagged
    assert PHONE_RE.search("call " + fake_phone).group(3).startswith("01")
    assert not PHONE_RE.search("version 1.2.3")
    assert EMAIL_RE.search("a" + "@" + "example.com").group(1) == "example.com"
    assert IPV4_RE.search("host " + ip + " here").group(1) == ip
    assert ACCOUNT_RE.search("acct " + "DU" + "1234567")
