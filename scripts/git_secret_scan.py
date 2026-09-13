#!/usr/bin/env python3
"""Refuse to publish anything that looks like a credential.

Scans every file git would commit (tracked + untracked, minus ignored) for the shapes of the
secrets this project handles. It is a tripwire, not a guarantee: it catches the obvious
accident — a token pasted into a doc, a .env copied under another name — which is exactly the
accident that costs the most.

    python scripts/git_secret_scan.py            # exit 1 on any hit
    python scripts/git_secret_scan.py --staged   # only what is currently staged

Patterns:
  * Telegram bot token        <8-12 digits>:<30-45 of [A-Za-z0-9_-]>
  * Binance API key / secret  64 chars of [A-Za-z0-9] containing BOTH cases (sha256 hashes are
                              lowercase hex only, so manifests do not trip this)
  * .env assignments with values  BIQ_*_KEY=..., *_SECRET=..., *_TOKEN=..., *_PASSWORD=...
                              (an empty value, as in .env.example, is fine)
  * forbidden paths           .env, .env.<anything except example>, *.pem, *.key
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TELEGRAM = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,45}\b")
KEY64 = re.compile(r"\b[A-Za-z0-9]{64}\b")
# [ \t]* on purpose, NOT \s*: \s would swallow the newline after "KEY=" and read the next
# line as the value, flagging every empty assignment in .env.example.
ENV_ASSIGN = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(BIQ_[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*)[ \t]*=[ \t]*(\S+)",
    re.MULTILINE,
)
# What a real value looks like: ASCII credential characters, 16+ long. Placeholders such as
# "...", "<token>", "발급받은키" or "your_key_here" do not qualify and are left alone.
CREDENTIAL_LIKE = re.compile(r"^[A-Za-z0-9_\-:.]{16,}$")
FORBIDDEN = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key)$")
ALLOWED_ENV = {".env.example"}
BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".tgz",
    ".gz",
    ".sqlite3",
    ".db",
}


def candidate_files(staged_only: bool) -> list[str]:
    if staged_only:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    else:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def scan(files: list[str]) -> list[str]:
    hits: list[str] = []
    for f in files:
        name = Path(f).name
        if FORBIDDEN.search(f) and name not in ALLOWED_ENV:
            hits.append(f"{f}: forbidden path (secrets file)")
            continue
        p = Path(f)
        if not p.is_file() or p.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in TELEGRAM.finditer(text):
            hits.append(f"{f}: telegram-token shape at offset {m.start()}")
        for m in KEY64.finditer(text):
            tok = m.group(0)
            if any(c.isupper() for c in tok) and any(c.islower() for c in tok):
                hits.append(f"{f}: 64-char mixed-case key shape at offset {m.start()}")
        for m in ENV_ASSIGN.finditer(text):
            var, val = m.group(1), m.group(2)
            if CREDENTIAL_LIKE.match(val.strip("'\"")) and "your_" not in val.lower():
                hits.append(f"{f}: {var}= has a value")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    args = ap.parse_args()
    files = candidate_files(args.staged)
    hits = scan(files)
    print(f"scanned {len(files)} files")
    if hits:
        print("SECRET SCAN FAILED:")
        for h in hits:
            print("  " + h)
        return 1
    print("secret scan clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
