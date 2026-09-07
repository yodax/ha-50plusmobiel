#!/usr/bin/env python3
"""Shared leak-scanning logic for the pre-commit and commit-msg hooks.

This repository is public and is developed against real household accounts. The
50+ Mobiel API's own responses carry live mobile numbers (`msisdns`) alongside
the handful of bundle figures the sensors actually want, and the portal logs in
with real email addresses. Every one of those is one careless `git add` — or one
careless commit message — away from being permanently public. GitHub keeps
deleted content reachable by commit SHA even after a force-push, so "notice it
later and remove it" is not a recovery path. Hence a preventive gate.

── Ported from ~/repos/ha-trappers/.githooks/, lessons included ──────────────

The four lessons below were paid for in that sibling repository, not this one.
They are restated rather than summarised because every one of them describes a
gate that *looked* installed and guarded nothing, and each is a mistake this
copy could repeat just as easily.

1. WHY PYTHON, NOT A grep PIPELINE. The first version filtered added lines with
   `grep -E '^\\+' | grep -v '^\\+\\+\\+'`. On this machine `grep` is ugrep, which
   rejects `^\\+\\+\\+` ("invalid syntax") — the pipeline errored, `|| true`
   swallowed it, and the hook exited 0 on a staged diff containing a live
   corporate email address. It was installed, looked right, and guarded nothing.
   Python's `re` has one dialect everywhere, and every subprocess failure below
   aborts the commit rather than passing it.

2. WHY THE SENSITIVE PATTERNS ARE NOT IN THIS FILE. The second version listed the
   real email address, postal code, phone number and employee numbers inline as
   patterns — and was correctly blocked by itself on the very first commit. A
   public repo cannot carry the list of strings it is guarding; the guard would
   be the leak. So this file holds only patterns that are generic infrastructure
   vocabulary (safe to publish), and loads the identity-specific ones from a file
   outside the repo.

3. WHY .githooks/ IS EXEMPT FROM THE GENERIC PATTERNS. v3 then blocked its own
   test fixtures — test-pre-commit.sh has to contain a sample LAN address in
   order to assert that LAN addresses are blocked. The exemption is scoped to
   .githooks/ and to the generic half only: identity patterns still apply there,
   because .githooks/ is exactly where lesson 2's mistake would land.

4. WHY COMMIT MESSAGES ARE SCANNED TOO. v4 checked only the staged diff. An agent
   working on that repo redacted a real account figure from the tree and from a
   published release note, and then described the same figure in the commit
   message that did the redacting — which the gate never looked at, and which
   cannot be recalled once pushed. A gate that covers the files but not the prose
   about the files is a gate with a door next to it.

── Two patterns this copy adds ──────────────────────────────────────────────

A Dutch mobile number and an email address, because those are what *this*
integration handles. Both need an allowlist, or the gate blocks the repo's own
documentation and test fixtures and gets switched off:

- Placeholder domains from RFC 2606/6761 (`example.com`, `*.invalid`, `*.test`,
  `*.localhost`, `*.example`) are exempt, as is the `noreply@` local part used
  by commit trailers. Everything else that looks like an address is blocked —
  including the maintainer's own, which is the point.
- The mobile-number patterns match the three shapes this API and its docs
  actually produce: `06xxxxxxxx`, `+316xxxxxxxx`/`0031...`, and the bare
  `316xxxxxxxx` msisdn form the GraphQL responses use.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Generic patterns. These are ordinary infrastructure vocabulary — publishing them
# reveals nothing about any particular person or network, so they live in the repo.
GENERIC_EXEMPT_PREFIX = ".githooks/"

# Domains that exist precisely so documentation can use them. Kept as one
# alternation so the email pattern below can exclude them in a lookahead.
_PLACEHOLDER_DOMAIN = (
    r"(?:[A-Za-z0-9-]+\.)*example\.(?:com|org|net|edu)"
    r"|(?:[A-Za-z0-9-]+\.)*(?:example|invalid|test|localhost)"
    r"|users\.noreply\.github\.com"
)

# The one filename shape that genuinely parses as an address: this repo's own
# docs describe `icon@2x.png` and `logo@2x.png`, i.e. local part `icon`, domain
# `2x.png`. Three tracked lines were flagged before this exemption existed, and
# a gate that cries wolf over an asset filename is a gate somebody turns off.
#
# The exemption is the retina-density marker itself — `@<digits>x` — and NOT a
# list of file extensions. An earlier version here excluded a suffix list
# containing `sh`, `md`, `py` and `zip`, every one of which is also a real TLD:
# `person@company.sh` sailed straight through, which is precisely the leak this
# pattern exists to catch. Anchoring on `2x` instead keeps `icon@2x.min.svg`
# exempt too, and leaves every real domain in scope.
_RETINA_ASSET = r"\d+x(?![A-Za-z0-9-])"

# An address, minus the placeholders. `(?!noreply@)` keeps the Co-Authored-By
# trailer git itself asks for from blocking every commit that carries one.
EMAIL_PATTERN = (
    r"\b(?!noreply@)[A-Za-z0-9._%+-]+@"
    r"(?!" + _RETINA_ASSET + r")"
    r"(?!(?:" + _PLACEHOLDER_DOMAIN + r")(?![A-Za-z0-9.-]))"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)

# Building blocks for the mobile-number patterns.
#
# `_SEP` is zero-or-more, not zero-or-one: a separator can fall between *any*
# pair of digits, and can repeat. "06-12 34 56 78", "06  1234 5678" and
# "316 1234 5678" were all missed while this was `[\s.-]?`.
#
# The boundaries exclude letters as well as digits. `\b` is useless in front of
# "+31" (two non-word characters have no boundary between them), and an earlier
# `(?<![\d+])` claimed to prevent matches inside a hex id but did not — it
# excluded digits only, so `hash=a0612345678b` was reported as a phone number.
_SEP = r"[\s.-]*"
_EIGHT_DIGITS = r"(?:" + _SEP + r"\d){8}"
_NOT_ADJACENT_BEFORE = r"(?<![0-9A-Za-z+])"
_NOT_ADJACENT_AFTER = r"(?![0-9A-Za-z])"

GENERIC = [
    (r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "private/loopback IP address"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "private LAN address"),
    (r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "private LAN address"),
    (r"pct\s+(?:exec|push|enter)", "Proxmox container command"),
    (r"\bproxmox\b", "hypervisor name"),
    (r"ssh\s+pve\b", "homelab SSH alias"),
    (r"/tank/docker", "homelab storage path"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", "JWT (a real session token)"),
    # Groups may be spaced ("NL91 ABNA 0417 1643 00"), which the inherited
    # unspaced-only pattern missed.
    (r"\bNL\d{2}\s?[A-Z]{4}(?:\s?\d){10}(?![0-9A-Za-z])", "IBAN"),
    (
        _NOT_ADJACENT_BEFORE + r"\(?06\)?" + _EIGHT_DIGITS + _NOT_ADJACENT_AFTER,
        "Dutch mobile number",
    ),
    (
        _NOT_ADJACENT_BEFORE
        + r"(?:\+|00)31"
        + _SEP
        + r"\(?0?\)?"
        + _SEP
        + r"6"
        + _EIGHT_DIGITS
        + _NOT_ADJACENT_AFTER,
        "Dutch mobile number (international form)",
    ),
    (
        _NOT_ADJACENT_BEFORE + r"316" + _EIGHT_DIGITS + _NOT_ADJACENT_AFTER,
        "Dutch mobile number (msisdn form)",
    ),
    (EMAIL_PATTERN, "email address"),
]

FIX_ADVICE = (
    "\n  Fix: replace with a placeholder (you@example.com, 06-XXXXXXXX), or move\n"
    "  the content into .claude/skills/deploy-test/ (gitignored) if it is deploy\n"
    "  tooling. Override only deliberately: SKIP_LEAK_CHECK=1 git commit ...\n\n"
)


def pattern_file_path() -> str:
    """Where the identity-specific patterns live — deliberately outside the repo.

    A maintainer who works on several of these integrations can point every repo
    at one list by symlinking, or by exporting MOBIEL50PLUS_LEAK_PATTERNS.
    """
    return os.environ.get(
        "MOBIEL50PLUS_LEAK_PATTERNS",
        os.path.join(
            os.path.expanduser("~"), ".config", "mobiel50plus", "leak-patterns.txt"
        ),
    )


def load_private(path: str) -> tuple[list[tuple[str, str]], bool]:
    """Load identity patterns from outside the repo. Returns (patterns, loaded).

    Aborts on a corrupt file rather than checking with fewer patterns, and — see
    below — aborts on a file that exists but defines nothing, which is
    indistinguishable from a mistake and would otherwise mean silently running
    with no identity coverage while every message claimed the private half was
    loaded.
    """
    if not os.path.exists(path):
        return [], False

    private: list[tuple[str, str]] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            rx, _, desc = line.partition("\t")
            rx = rx.strip()
            if not rx:
                continue
            try:
                re.compile(rx)
            except re.error as exc:
                sys.stderr.write(
                    "leak check: %s line %d is not a valid regex (%s).\n"
                    "Refusing the commit rather than checking with a broken pattern "
                    "list.\n" % (path, lineno, exc)
                )
                sys.exit(1)
            private.append((rx, desc.strip() or "private pattern"))

    if not private:
        sys.stderr.write(
            "leak check: %s exists but defines no patterns.\n"
            "Refusing the commit: an empty identity list is indistinguishable from a\n"
            "mistake, and running with none of them while reporting the private half as\n"
            "loaded is exactly the fail-open this gate exists to prevent.\n"
            "Delete the file to run generic-only deliberately.\n" % path
        )
        sys.exit(1)

    return private, True


def run(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(
            "leak check: could not run (%s exited %d):\n%s\n"
            "Refusing the commit rather than passing unchecked.\n"
            % (" ".join(args), p.returncode, p.stderr.strip())
        )
        sys.exit(1)
    return p.stdout


def added_lines_from_staged_diff() -> list[tuple[str, str]]:
    """Every added line in the staged diff, as (path, text).

    The hunk-state machine is load-bearing. A previous version treated any line
    starting with `+++ ` as the diff's file header — but a *content* line reading
    `++ something` is rendered as `+++ something` in the diff, so an identity
    canary on such a line was silently skipped as metadata. `+++ ` is only a
    header before the first `@@` hunk marker of a file; after it, every `+` line
    is content.
    """
    # -z: NUL-delimited and unquoted. With the default `core.quotePath`, plain
    # --name-only renders "café.txt" as the literal C-escaped string
    # "caf\303\251.txt"; feeding that back as a pathspec matches no file, and an
    # unmatched pathspec yields an empty diff rather than an error — so the file
    # would be committed unscanned.
    staged = [
        f
        for f in run(
            ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"]
        ).split("\0")
        if f
    ]
    if not staged:
        return []

    # -U0: only changed lines. Renames and mode changes produce no '+' lines at
    # all, which is correct — they introduce no new content that could leak.
    diff = run(["git", "diff", "--cached", "-U0", "--"] + staged)

    current_file: str | None = None
    in_hunk = False
    added: list[tuple[str, str]] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk and line.startswith("+++ "):
            path = line[4:].strip()
            current_file = path[2:] if path.startswith("b/") else path
            continue
        if in_hunk and line.startswith("+"):
            added.append((current_file or "<unknown>", line[1:]))
    return added


# The banner names which gate fired. Both hooks share this scanner, and an
# earlier version had them print byte-identical text — so a harness case
# asserting "the staged-content gate caught this" was equally satisfied by the
# *message* gate firing on a fixture that happened to carry the same string in
# both places. The secret would never have been in the staged diff, the content
# scanner would never have matched, and the test would still go green. Naming
# the scope also spares a blocked human working out which of two hooks stopped
# them.
CONTENT_SCOPE = "staged changes"
MESSAGE_SCOPE = "commit message"


def scan(
    entries: list[tuple[str, str]],
    private: list[tuple[str, str]],
    *,
    apply_generic_exemption: bool,
    scope_label: str,
) -> bool:
    """Report any match. Returns True if something was blocked."""
    failed = False
    for pattern, why, generic in (
        [(p, w, True) for p, w in GENERIC] + [(p, w, False) for p, w in private]
    ):
        rx = re.compile(pattern, re.IGNORECASE)
        in_scope = [
            (f, t)
            for f, t in entries
            if not (
                generic
                and apply_generic_exemption
                and f.startswith(GENERIC_EXEMPT_PREFIX)
            )
        ]
        hits = [(f, t) for f, t in in_scope if rx.search(t)]
        if not hits:
            continue
        if not failed:
            sys.stderr.write(
                "\nleak check: BLOCKED (%s) — this commit contains content that must "
                "not be published.\n\n" % scope_label
            )
            failed = True
        sys.stderr.write("  %s:\n" % why)
        for f, t in hits[:5]:
            sys.stderr.write("    %s: %s\n" % (f, t.strip()[:120]))
        if len(hits) > 5:
            sys.stderr.write("    ... and %d more\n" % (len(hits) - 5))
    return failed


def warn_if_generic_only(loaded_private: bool, path: str) -> None:
    """Say plainly which half ran.

    A contributor cloning this repo has no identity-specific pattern file and no
    secrets of the maintainer's to leak, so generic-only is right for them — but
    it must never look like full coverage to the maintainer, whose file has
    simply gone missing.
    """
    if not loaded_private:
        sys.stderr.write(
            "leak check: generic patterns only — no identity pattern file at %s.\n"
            "  (Expected for an outside contributor. If you are the maintainer, restore it:\n"
            "   the identity-specific patterns are deliberately not stored in this repo.)\n"
            % path
        )


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "pre-commit"
    path = pattern_file_path()
    private, loaded = load_private(path)

    if mode == "commit-msg":
        message_file = argv[2]
        with open(message_file, encoding="utf-8", errors="replace") as fh:
            # EVERY line, '#' ones included. An earlier version skipped them on
            # the assumption that git strips comments — but `git commit -m` and
            # `-F` use `cleanup=whitespace` by default, which does not, so
            # `# ZZQQ-CANARY-4711` sailed straight through the gate and into the
            # published message. What gets committed is what gets scanned.
            entries = [("commit message", line.rstrip("\n")) for line in fh]
        # No path, so no .githooks/ exemption applies: a commit message has no
        # legitimate reason to contain a sample LAN address.
        failed = scan(
            entries, private, apply_generic_exemption=False, scope_label=MESSAGE_SCOPE
        )
    else:
        failed = scan(
            added_lines_from_staged_diff(),
            private,
            apply_generic_exemption=True,
            scope_label=CONTENT_SCOPE,
        )

    if failed:
        sys.stderr.write(FIX_ADVICE)
        return 1

    warn_if_generic_only(loaded, path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
