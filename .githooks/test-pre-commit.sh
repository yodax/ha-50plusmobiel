#!/bin/bash
# test-pre-commit.sh — proves .githooks/pre-commit actually blocks, rather than
# assuming it does.
#
# WHY THIS EXISTS: in the sibling ha-trappers repo this gate was ported from, the hook's
# first version used a grep pipeline that errored out under ugrep (this machine's `grep`)
# and, with `|| true` swallowing the error, exited 0 on a staged diff containing a live
# corporate email address. It looked installed and correct and guarded nothing. A leak
# guard is only worth having if something checks it — and an untested one is worse than
# none, because it is trusted.
#
# Every string below is synthetic. The identity-specific patterns the real hook also
# loads are deliberately NOT in this repo (see the hook's header), so this script tests
# the *loading mechanism* with a throwaway pattern file and a canary string instead —
# which proves the private half works without publishing any of it.
#
# Run from the repo root:  .githooks/test-pre-commit.sh
# Exits 0 only if every case behaves as expected.
set -uo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel) || exit 1
HOOK="$REPO_ROOT/.githooks/pre-commit"
[ -x "$HOOK" ] || { echo "FAIL: $HOOK is not executable"; exit 1; }

WORK=$(mktemp -d) || { echo "FAIL: could not create a scratch directory"; exit 1; }
[ -d "$WORK" ] || { echo "FAIL: scratch directory missing"; exit 1; }
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0
case_n=0

# Each case gets a FRESH scratch repo. An earlier draft reused one, and state left over
# from prior cases (a deleted HEAD plus a still-populated index) made a later `git commit`
# find nothing to commit — which the assertion then read as "the hook blocked it". A test
# harness that reports the wrong reason is the same fail-open trap the hook itself fell
# into; isolation is cheaper than interpreting the residue.
fresh_repo() {
  case_n=$((case_n + 1))
  SCRATCH="$WORK/case$case_n"
  git init -q -b main "$SCRATCH" || {
    echo "FAIL: could not create scratch repo (harness broken, not a hook verdict)"
    exit 1
  }
  git -C "$SCRATCH" config user.name "test"
  git -C "$SCRATCH" config user.email "test@example.com"
  git -C "$SCRATCH" config core.hooksPath "$REPO_ROOT/.githooks"
}

# Default: no identity pattern file, so only the generic half is exercised. Individual
# cases override MOBIEL50PLUS_LEAK_PATTERNS where they need the private half.
export MOBIEL50PLUS_LEAK_PATTERNS="$WORK/no-such-pattern-file.txt"

# _commit <content> → 0 if the commit went through, 1 if the hook blocked it.
# Output of the attempt is left in $LAST_OUT for assertions that inspect it.
_commit() {
  fresh_repo
  local path="${PROBE_PATH:-probe.txt}"
  mkdir -p "$SCRATCH/$(dirname "$path")"
  printf '%s\n' "$1" > "$SCRATCH/$path"
  git -C "$SCRATCH" add "$path"
  local rc=0
  LAST_OUT=$(git -C "$SCRATCH" commit -m "probe" 2>&1) || rc=1
  # A commit that produced no new commit is a harness bug, not a hook verdict.
  if [ "$rc" -eq 0 ] && ! git -C "$SCRATCH" rev-parse --verify -q HEAD >/dev/null; then
    echo "HARNESS BUG: commit reported success but created no commit:"
    printf '%s\n' "$LAST_OUT" | sed 's/^/    /'
    return 2
  fi
  return "$rc"
}

# _commit_msg <message> → 0 if the commit went through, 1 if a hook blocked it.
# Content is benign; only the message varies, so this isolates the commit-msg hook.
_commit_msg() {
  fresh_repo
  printf 'nothing to see here\n' > "$SCRATCH/probe.txt"
  git -C "$SCRATCH" add probe.txt
  local rc=0
  LAST_OUT=$(git -C "$SCRATCH" commit -m "$1" 2>&1) || rc=1
  if [ "$rc" -eq 0 ] && ! git -C "$SCRATCH" rev-parse --verify -q HEAD >/dev/null; then
    echo "HARNESS BUG: commit reported success but created no commit:"
    printf '%s\n' "$LAST_OUT" | sed 's/^/    /'
    return 2
  fi
  return "$rc"
}

msg_should_block() {
  local rc=0
  _commit_msg "$2" || rc=$?
  case "$rc" in
    0) echo "FAIL: commit-msg did NOT block: $1"; fail=$((fail + 1)) ;;
    1) if printf '%s' "$LAST_OUT" | grep -qF "${EXPECT_MARKER:-$MSG_BLOCK_MARKER}"; then
         echo "ok:   blocked $1"; pass=$((pass + 1))
       else
         echo "FAIL: commit failed, but not with ${EXPECT_MARKER:-$MSG_BLOCK_MARKER}: $1"
         printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; fail=$((fail + 1))
       fi ;;
    *) echo "FAIL: harness error on: $1"; fail=$((fail + 1)) ;;
  esac
}

msg_should_pass() {
  local rc=0
  _commit_msg "$2" || rc=$?
  case "$rc" in
    0) echo "ok:   allowed $1"; pass=$((pass + 1)) ;;
    1) echo "FAIL: commit-msg blocked a clean message: $1"
       printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; fail=$((fail + 1)) ;;
    *) echo "FAIL: harness error on: $1"; fail=$((fail + 1)) ;;
  esac
}

# A commit can fail for reasons that have nothing to do with the hook — a broken
# scratch repo, a missing mktemp, a git that would not run. Counting any failure
# as "blocked" is the same fail-open the hook itself once had, one level up. So a
# block only counts if the scanner said so in its own words.
#
# The marker is the BLOCKED banner *with its scope*, and two earlier versions of
# it were both too loose:
#
#   "leak check:"          — also printed by the SUCCESS path's "generic patterns
#                            only" warning and by the pattern-file abort messages,
#                            so any unrelated crash that printed it satisfied
#                            should_block() with nothing detected.
#   "leak check: BLOCKED"  — narrower, but identical for both hooks, so a case
#                            asserting the staged-content gate fired was equally
#                            satisfied by the commit-MESSAGE gate firing on a
#                            fixture carrying the same string in both places.
#
# Each gate now names itself, and each case asserts the specific one. The two
# abort paths carry their own EXPECT_MARKER rather than loosening these.
# `test_scope_markers_are_distinguishable` below reproduces the false pass and
# fails if it ever returns.
BLOCK_MARKER="leak check: BLOCKED (staged changes)"
MSG_BLOCK_MARKER="leak check: BLOCKED (commit message)"

should_block() {
  local rc=0
  _commit "$2" || rc=$?
  case "$rc" in
    0) echo "FAIL: hook did NOT block: $1"; fail=$((fail + 1)) ;;
    1) if printf '%s' "$LAST_OUT" | grep -qF "${EXPECT_MARKER:-$BLOCK_MARKER}"; then
         echo "ok:   blocked $1"; pass=$((pass + 1))
       else
         echo "FAIL: commit failed, but not with ${EXPECT_MARKER:-$BLOCK_MARKER}: $1"
         printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; fail=$((fail + 1))
       fi ;;
    *) echo "FAIL: harness error on: $1"; fail=$((fail + 1)) ;;
  esac
}

should_pass() {
  local rc=0
  _commit "$2" || rc=$?
  case "$rc" in
    0) echo "ok:   allowed $1"; pass=$((pass + 1)) ;;
    1) echo "FAIL: hook blocked a clean commit: $1"
       printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; fail=$((fail + 1)) ;;
    *) echo "FAIL: harness error on: $1"; fail=$((fail + 1)) ;;
  esac
}

echo "── generic patterns (shipped in the hook) ──"
should_block "RFC1918 192.168 address"  'the box lives at 192.168.8.60'
should_block "RFC1918 10.x address"     'coordinator at 10.4.2.9'
should_block "RFC1918 172.16 address"   'gateway 172.20.0.1'
should_block "loopback address"         'bind to 127.0.0.1:8123'
should_block "pct exec"                 'run pct exec 100 -- docker restart ha'
should_block "hypervisor name"          'copy it onto the Proxmox host first'
should_block "homelab ssh alias"        'ssh pve then restart'
should_block "homelab storage path"     'config at /tank/docker/homeassistant'
should_block "a real JWT" \
  'token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE3ODAwMDAwMDB9.AbCdEfGhIjK'
should_block "IBAN"                     'ibanAccountNumber: NL91ABNA0417164300'

echo "── benign content must still commit ──"
should_pass  "placeholder email"        'username: you@example.com'
should_pass  "sanitized API sample"     '{"dataAvailable": 9433, "msisdn": "06-XXXXXXXX"}'
should_pass  "ordinary python"          'async def async_get_status(self) -> dict:'
should_pass  "public API host"          'BASE_URL = "https://api.trappers.net/api/"'
should_pass  "a version-looking number" 'MIN_HA_VERSION = "2026.3.0"'

echo "── this repo's own additions: mobile numbers and email addresses ──"
should_block "Dutch mobile, 06 form"     'msisdn 0612345678 belongs to the account'
should_block "Dutch mobile, dashed"      'call 06-12 34 56 78 to verify'
should_block "Dutch mobile, +31 form"    'number is +31612345678'
should_block "Dutch mobile, 0031 form"   'number is 0031612345678'
should_block "Dutch mobile, (0) form"    'number is +31 (0)6 1234 5678'
should_block "msisdn as the API returns it" '{"msisdn": "31612345678"}'
should_block "a real-looking email"      'username: someone@gmail.com'
should_block "a real-looking work email" 'contact: firstname.lastname@some-company.nl'

echo "── documentation placeholders must still commit ──"
should_pass  "RFC2606 example.com"       'CONF_USERNAME: user@example.com'
should_pass  "RFC2606 example.org"       'author: nobody@example.org'
should_pass  "RFC6761 .invalid"          'unique_id="michael@example.invalid"'
should_pass  "RFC6761 .test"             'login as dean@kroes.test'
should_pass  "a .example TLD"            'login as sam@kroes.example'
should_pass  "a subdomain of example.com" 'mail to a@mail.example.com'
should_pass  "the Co-Authored-By trailer" 'Co-Authored-By: Claude <noreply@anthropic.com>'
should_pass  "a GitHub noreply address"  'someone@users.noreply.github.com'
should_pass  "a CODEOWNERS handle"       '"codeowners": ["@yodax"]'
should_pass  "a retina asset filename"   'ships icon@2x.png and logo@2x.png'
should_pass  "an @-prefixed npm scope"   'installed @scope/pkg.js from the CDN'
should_block "an address ending in a real TLD" 'mail someone@kroes.email about it'
should_pass  "a redacted phone number"   'msisdn: 06-XXXXXXXX'
should_pass  "an ISO timestamp with 06"  'startDate: 2026-08-29T06:12:34Z'
should_pass  "a bundle figure"           '"dataAvailable": 9433, "dataAssigned": 12000'
should_pass  "the HA version floor"      '"homeassistant": "2026.3.0"'
should_pass  "the public oauth client id" 'OAUTH_CLIENT_ID = "e80df68638c3ba8264d0d762da442ee0"'
should_pass  "the portal hostname"       'VERIFY_LOGIN_URL = "https://mijn.50plusmobiel.nl/verifyLogin"'

echo "── an address in a commit message is a leak too ──"
msg_should_block "an email address in the message" 'tested with someone@gmail.com'
msg_should_block "a mobile number in the message"  'read back msisdn 0612345678'
msg_should_pass  "a placeholder in the message"    'documented as user@example.com'
msg_should_pass  "the Co-Authored-By trailer"      'Fix titles

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>'

echo "── regressions from the independent review of this gate ──"
# An earlier version exempted a list of file extensions from the email pattern.
# sh, md, py and zip are all real TLDs, so these four addresses were invisible.
should_block "address on the .sh TLD"    'mail person@company.sh'
should_block "address on the .md TLD"    'mail person@company.md'
should_block "address on the .py TLD"    'mail person@company.py'
should_block "address on the .zip TLD"   'mail person@company.zip'
should_pass  "a multi-suffix retina asset" 'built icon@2x.min.svg from the source'
# Separators can repeat and can sit anywhere; parens are a common Dutch form.
should_block "mobile in (06) form"       'bel (06) 12345678 voor vragen'
should_block "mobile with a double space" 'nummer 06  1234 5678'
should_block "spaced msisdn form"        'msisdn 316 1234 5678'
# The old boundary excluded digits but not letters, so a hex id looked like one.
should_pass  "a hex id containing 06..." 'hash=a0612345678b'
should_pass  "a git sha"                 'commit 0612345678abcdef0612345678abcdef06123456'
# The inherited IBAN pattern only matched the unspaced form.
should_block "a spaced IBAN"             'iban NL91 ABNA 0417 1643 00'

echo "── identity pattern file (the private half) ──"
CANARY_FILE="$WORK/patterns.txt"
printf '# synthetic\nZZQQ-CANARY-[0-9]{4}\tsynthetic canary\n' > "$CANARY_FILE"

MOBIEL50PLUS_LEAK_PATTERNS="$CANARY_FILE" should_block "canary from pattern file" \
  'secret marker ZZQQ-CANARY-4711 here'
MOBIEL50PLUS_LEAK_PATTERNS="$CANARY_FILE" should_pass "non-matching line with file loaded" \
  'secret marker ZZQQ-CANARY-not-a-number here'

# A corrupt pattern file must abort, not silently check with fewer patterns — the same
# fail-open shape that made the first version of this hook worthless.
BAD_FILE="$WORK/bad-patterns.txt"
printf 'ZZQQ-[unclosed\tbroken regex\n' > "$BAD_FILE"
EXPECT_MARKER="is not a valid regex" MOBIEL50PLUS_LEAK_PATTERNS="$BAD_FILE" \
  should_block "corrupt pattern file aborts the commit" 'entirely harmless line'

# Missing pattern file: the generic half still runs (a contributor has no secrets of the
# maintainer's to leak), and the hook says so rather than implying full coverage.
if MOBIEL50PLUS_LEAK_PATTERNS="$WORK/definitely-absent.txt" _commit 'harmless' &&
   printf '%s\n' "$LAST_OUT" | grep -q "generic patterns only"; then
  echo "ok:   missing pattern file warns instead of implying full coverage"
  pass=$((pass + 1))
else
  echo "FAIL: missing pattern file did not warn; output was:"
  printf '%s\n' "${LAST_OUT:-<none>}" | sed 's/^/    /'
  fail=$((fail + 1))
fi

echo "── .githooks/ exemption is asymmetric ──"
# The hook and this file must be able to contain generic patterns — they define and
# exercise them. Without this exemption the gate blocks its own test fixtures, which is
# exactly what happened on the third commit attempt.
PROBE_PATH=".githooks/fixture.sh" should_pass "generic pattern inside .githooks/" \
  'should_block "LAN" "the box lives at 192.168.8.60"'
# But the exemption must NOT cover identity patterns: putting a real secret in the hook
# is the v2 mistake, and .githooks/ is precisely where it would land.
PROBE_PATH=".githooks/fixture.sh" MOBIEL50PLUS_LEAK_PATTERNS="$CANARY_FILE" \
  should_block "identity pattern inside .githooks/ is still caught" \
  'ZZQQ-CANARY-4711'
# And outside .githooks/, generic patterns still apply.
PROBE_PATH="custom_components/mobiel50plus/api.py" should_block \
  "generic pattern outside .githooks/ still blocked" 'HOST = "192.168.8.60"'

echo
echo "── a '++' content line is content, not a diff header ──"
# A staged line reading "++ x" renders as "+++ x" in the diff. Treating any "+++ "
# line as the file header let an identity canary on such a line through unchecked.
CANARY_FILE2="$WORK/patterns2.txt"
printf '# synthetic\nZZQQ-CANARY-[0-9]{4}\tsynthetic canary\n' > "$CANARY_FILE2"
MOBIEL50PLUS_LEAK_PATTERNS="$CANARY_FILE2" \
  should_block "canary on a line beginning with ++" '++ ZZQQ-CANARY-1234'
MOBIEL50PLUS_LEAK_PATTERNS="$CANARY_FILE2" \
  should_block "canary on a line beginning with +++" '+++ ZZQQ-CANARY-1234'
should_pass  "an ordinary ++ line with nothing secret" '++ just a diff-looking line'

echo "── an existing but empty pattern file must not pass silently ──"
EMPTY_FILE="$WORK/empty-patterns.txt"
printf '# only comments, no patterns\n\n' > "$EMPTY_FILE"
EXPECT_MARKER="exists but defines no patterns" MOBIEL50PLUS_LEAK_PATTERNS="$EMPTY_FILE" \
  should_block "empty pattern file aborts rather than running with no identity cover" \
  'perfectly ordinary content'

echo "── commit messages are scanned too ──"
msg_should_pass  "an ordinary commit message" 'Fix the paging offset'
msg_should_block "a LAN address in the message" 'Deploy tested against 192.168.8.60'
msg_should_block "a homelab path in the message" 'copied into /tank/docker/homeassistant'
msg_should_block "an IBAN in the message" 'removed NL91ABNA0417164300 from the fixture'
MOBIEL50PLUS_LEAK_PATTERNS="$CANARY_FILE2" \
  msg_should_block "an identity canary in the message" 'redacted ZZQQ-CANARY-1234 from README'

echo "── a '#' line in a commit message is still published ──"
# git commit -m and -F use cleanup=whitespace, which does NOT strip comments.
msg_should_block "a commented-out LAN address in the message" '# staged on 192.168.8.60'
MOBIEL50PLUS_LEAK_PATTERNS="$CANARY_FILE2" \
  msg_should_block "a commented-out identity canary" '# ZZQQ-CANARY-1234'

echo "── a non-ASCII filename must still be scanned ──"
# With core.quotePath on, --name-only renders "café.txt" C-escaped; feeding that
# back as a pathspec matches nothing and yields an empty, unscanned diff.
PROBE_PATH='café.txt' should_block "LAN address in a non-ASCII filename" \
  'the box lives at 192.168.8.60'
unset PROBE_PATH

echo "── the two gates must be distinguishable (meta-check) ──"
# Reproduces the false pass this marker scheme exists to prevent: a commit whose
# staged content is CLEAN but whose MESSAGE carries a secret. Under a shared
# marker, a staged-content assertion accepted this. It must now be reported as a
# commit-message block and must NOT satisfy the staged-content marker.
fresh_repo
printf 'nothing to see here\n' > "$SCRATCH/probe.txt"
git -C "$SCRATCH" add probe.txt
META_OUT=$(git -C "$SCRATCH" commit -m 'staged on 192.168.8.60' 2>&1) || true
if printf '%s' "$META_OUT" | grep -qF "$MSG_BLOCK_MARKER" &&
   ! printf '%s' "$META_OUT" | grep -qF "$BLOCK_MARKER"; then
  echo "ok:   a message-only secret reports as the commit-message gate, not content"
  pass=$((pass + 1))
else
  echo "FAIL: the two gates are not distinguishable; output was:"
  printf '%s\n' "$META_OUT" | sed 's/^/    /'
  fail=$((fail + 1))
fi

# And the inverse: a content-only secret must not claim the message gate fired.
fresh_repo
printf 'host 192.168.8.60\n' > "$SCRATCH/probe.txt"
git -C "$SCRATCH" add probe.txt
META_OUT=$(git -C "$SCRATCH" commit -m 'an entirely clean message' 2>&1) || true
if printf '%s' "$META_OUT" | grep -qF "$BLOCK_MARKER" &&
   ! printf '%s' "$META_OUT" | grep -qF "$MSG_BLOCK_MARKER"; then
  echo "ok:   a content-only secret reports as the staged-content gate, not message"
  pass=$((pass + 1))
else
  echo "FAIL: the two gates are not distinguishable; output was:"
  printf '%s\n' "$META_OUT" | sed 's/^/    /'
  fail=$((fail + 1))
fi

echo
echo "pre-commit leak-guard: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
