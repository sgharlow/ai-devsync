#!/bin/sh
# A stranger's first hour with devsync, following README.md as published on origin/main.
# Runs in a container that has only python, git and pipx. Every command a README code block
# shows is run as written; the "PCs" are separate HOME directories sharing one bare store repo.
# Prints PASS/FAIL per step and exits non-zero if any step failed.
fails=0
step() { printf '\n== %s\n' "$1"; }
ok()   { printf 'PASS  %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }
expect() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (got '$1', wanted '$2')"; fi; }
has()  { if printf '%s' "$1" | grep -q -- "$2"; then ok "$3"; else bad "$3 (missing '$2')"; fi; }
lacks(){ if printf '%s' "$1" | grep -q -- "$2"; then bad "$3 (found '$2')"; else ok "$3"; fi; }

export PIPX_HOME="${PIPX_HOME:-/opt/pipx}" PIPX_BIN_DIR="${PIPX_BIN_DIR:-/usr/local/bin}"
export PATH="$PIPX_BIN_DIR:$PATH"
ROOT="${WALK_ROOT:-/srv/devsync-walk}"; mkdir -p "$ROOT/home"
STORE=$ROOT/store.git; STORE2=$ROOT/store2.git
git init -q --bare --initial-branch=main "$STORE"; git init -q --bare --initial-branch=main "$STORE2"

key() { printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'; }   # Claude Code's project-key naming

as() {  # as <name> : become that "PC"
  export HOME="$ROOT/home/$1"; mkdir -p "$HOME"; cd "$HOME"
  [ -f "$HOME/.gitconfig" ] || { git config --global user.name "$1"; git config --global user.email "$1@example.invalid"; }
}

step "Install (README: Install)"
# CI installs the checkout under test; by hand it installs what the README names.
SPEC="${DEVSYNC_INSTALL_SPEC:-git+https://github.com/sgharlow/ai-devsync.git}"
out=$(pipx install "$SPEC" 2>&1); rc=$?
expect "$rc" 0 "pipx install from the README line"
[ $rc -ne 0 ] && printf '%s\n' "$out" | tail -5
v=$(devsync --version 2>&1); has "$v" "devsync 0.2" "devsync is on PATH and reports a version"
v=$("$PIPX_HOME/venvs/ai-devsync/bin/python" -m devsync --version 2>&1); has "$v" "devsync 0.2" "python -m devsync works"
v=$(ai-devsync --version 2>&1); has "$v" "devsync 0.2" "the ai-devsync command is an alias"

step "First machine (README: First machine) -- no flags except --remote"
as alice
AKEY=$(key "$HOME/code")
mkdir -p code/app ".claude/projects/$AKEY/memory" .claude/skills/mine
printf 'alice global\nshared line\nfooter\n' > .claude/CLAUDE.md
printf 'alice index\n' > ".claude/projects/$AKEY/memory/MEMORY.md"
printf 'my skill\n' > .claude/skills/mine/SKILL.md
mkdir -p .codex .kiro/steering .claude/agents
printf 'reviewer agent
' > .claude/agents/reviewer.md
printf 'codex rules\n' > .codex/AGENTS.md
printf '{"token":"LOGINSECRET"}\n' > .codex/auth.json
printf 'kiro steering\n' > .kiro/steering/style.md
(cd code/app && git init -q --initial-branch=main && printf '.env\n' > .gitignore && printf 'TOKEN=abc123secret\n' > .env)
out=$(devsync init --seed --remote "$STORE" 2>&1); rc=$?
expect "$rc" 0 "init --seed with defaults"
has "$out" "config and memory only" "it says secrets are not synced by default"
has "$(git --git-dir=$STORE show main:claude/global/CLAUDE.md 2>&1)" "alice global" "CLAUDE.md reached the store"
has "$(git --git-dir=$STORE show main:claude/memory/projects/MEMORY.md 2>&1)" "alice index" "memory reached the store under a neutral name"
lacks "$(git --git-dir=$STORE ls-tree -r --name-only main)" "repos/" "no gitignored repo file was uploaded"
has "$(git --git-dir=$STORE show main:agents/codex/AGENTS.md 2>&1)" "codex rules" "Codex instructions reached the store"
has "$(git --git-dir=$STORE show main:agents/kiro/steering/style.md 2>&1)" "kiro steering" "Kiro steering reached the store"
lacks "$(git --git-dir=$STORE ls-tree -r --name-only main)" "auth.json" "the Codex login file was not uploaded"
cfg=$(devsync config 2>&1); has "$cfg" "$ROOT/home/alice/code" "root was found at ~/code without a flag"

step "Second machine (README: Every other machine)"
as bob
mkdir -p code .claude .codex      # bob uses Codex but not Kiro
printf 'bob global\n' > .claude/CLAUDE.md
out=$(devsync adopt --remote "$STORE" 2>&1); rc=$?
expect "$rc" 3 "plain adopt stops when this PC differs"
has "$out" "--follower" "it names --follower"; has "$out" "--merge" "it names --merge"
expect "$(cat .claude/CLAUDE.md)" "bob global" "nothing on this PC was changed by the refusal"
out=$(devsync adopt --follower --remote "$STORE" 2>&1); rc=$?
expect "$rc" 0 "adopt --follower"
has "$(cat .claude/CLAUDE.md)" "alice global" "bob now has the store's CLAUDE.md"
expect "$(cat ".claude/projects/$(key "$HOME/code")/memory/MEMORY.md" 2>&1)" "alice index" "memory landed under bob's own project key"
expect "$(cat .claude/skills/mine/SKILL.md 2>&1)" "my skill" "skills came down"
expect "$(cat .codex/AGENTS.md 2>&1)" "codex rules" "Codex instructions came down"
expect "$(cat .claude/agents/reviewer.md 2>&1)" "reviewer agent" "the agents folder came down, not only skills"
[ -d .kiro ] && bad "a folder was created for an agent bob does not use" || ok "no folder invented for an unused agent"
has "$(devsync home list 2>&1)" '"name": "gemini"' "home list shows the known agents"
out=$(devsync restore claude/global/CLAUDE.md 2>&1); has "$(cat .claude/CLAUDE.md)" "bob global" "the set-aside file is recoverable with restore"
printf 'alice global\nshared line\nfooter\n' > .claude/CLAUDE.md

step "Daily use (README: Daily use) -- two-sided edit merges"
printf 'alice global\nshared line\nfooter\nadded by bob\n' > .claude/CLAUDE.md
out=$(devsync sync 2>&1); expect "$?" 0 "bob syncs his edit"
as alice
printf 'added by alice\nalice global\nshared line\nfooter\n' > .claude/CLAUDE.md
out=$(devsync sync 2>&1); expect "$?" 0 "alice syncs her edit"
m=$(cat .claude/CLAUDE.md); has "$m" "added by bob" "alice has bob's line"; has "$m" "added by alice" "and kept her own"
st=$(devsync status 2>&1); has "$st" '"behind": 0' "status reports in sync"

step "Conflicts (README: Conflicts) -- default policy settles it"
as bob; devsync sync >/dev/null 2>&1
sed -i 's/shared line/shared line (bob wording)/' .claude/CLAUDE.md; devsync sync >/dev/null 2>&1
as alice
sed -i 's/shared line/shared line (alice wording)/' .claude/CLAUDE.md; touch -d '+1 minute' .claude/CLAUDE.md
out=$(devsync sync 2>&1); rc=$?
expect "$rc" 0 "a same-line conflict does not stop the sync"
has "$out" "autoresolved=1" "the summary says it settled one"
lacks "$(cat .claude/CLAUDE.md)" "<<<<<<<" "no conflict markers in the file"
[ -f .claude/CLAUDE.md.conflict ] && bad "a sidecar was written" || ok "no sidecar"
n=$(find .devsync/trash -name 'CLAUDE.md.loser' | wc -l); expect "$n" 1 "the other side is kept in trash"
has "$(devsync conflicts 2>&1)" "no unresolved conflicts" "conflicts lists nothing"

step "Conflicts with policy ask + devsync resolve"
out=$(devsync config set conflict-policy ask 2>&1); expect "$?" 0 "config set conflict-policy ask"
devsync sync >/dev/null 2>&1
as bob; devsync sync >/dev/null 2>&1
sed -i 's/footer/footer (bob)/' .claude/CLAUDE.md; devsync sync >/dev/null 2>&1
as alice
sed -i 's/footer/footer (alice)/' .claude/CLAUDE.md
out=$(devsync sync 2>&1); expect "$?" 2 "policy ask: sync exits 2 on a conflict"
[ -f .claude/CLAUDE.md.conflict ] && ok "sidecar written" || bad "no sidecar"
out=$(devsync resolve --all --keep remote 2>&1); expect "$?" 0 "resolve --all --keep remote"
m=$(cat .claude/CLAUDE.md); has "$m" "footer (bob)" "kept the remote side"; lacks "$m" "<<<<<<<" "markers gone"
out=$(devsync sync 2>&1); expect "$?" 0 "the conflict does not come back"

step "Explicit delete and restore"
printf 'scratch\n' > .claude/skills/mine/notes.md; devsync sync >/dev/null 2>&1
out=$(devsync rm claude/skills/mine/notes.md 2>&1); expect "$?" 0 "rm"
[ -f .claude/skills/mine/notes.md ] && bad "file still present after rm" || ok "file removed locally"
as bob; devsync sync >/dev/null 2>&1
[ -f .claude/skills/mine/notes.md ] && bad "delete did not reach bob" || ok "the delete reached the other PC"

step "Edits on two machines before either syncs"
as alice
printf 'a\n' > .claude/skills/mine/a.md; printf 'b\n' > .claude/skills/mine/b.md; devsync sync >/dev/null 2>&1
as bob; devsync sync >/dev/null 2>&1
as alice
out=$(devsync rm claude/skills/mine/a.md 2>&1); expect "$?" 0 "alice deletes one file"
as bob
out=$(devsync rm claude/skills/mine/b.md 2>&1); expect "$?" 0 "bob deletes another before seeing hers: no wedge"
out=$(devsync sync 2>&1); expect "$?" 0 "and bob's next sync still works"
[ -f .claude/skills/mine/a.md ] && bad "alice's delete did not reach bob" || ok "alice's delete reached bob"
out=$(devsync config set project-key-prefix -home-somewhere-code 2>&1); expect "$?" 0 "a setting whose value starts with a dash is accepted"
devsync config set project-key-prefix "$(key "$HOME/code")" >/dev/null 2>&1
out=$(devsync rm claude/skills/mine 2>&1); expect "$?" 2 "rm of a folder is refused"
as gina       # joins a store nobody has seeded
mkdir -p code .claude; git init -q --bare --initial-branch=main "$ROOT/unseeded.git"
out=$(devsync adopt --remote "$ROOT/unseeded.git" 2>&1); expect "$?" 1 "adopting an unseeded store is refused"
lacks "$(git --git-dir=$ROOT/unseeded.git for-each-ref 2>&1)" "refs/heads" "and nothing was pushed to it"
as hank       # the first machine seeds it afterwards
mkdir -p code .claude; printf 'hank global\n' > .claude/CLAUDE.md
out=$(devsync init --seed --remote "$ROOT/unseeded.git" 2>&1); expect "$?" 0 "the store is seeded later"
as gina
out=$(devsync adopt --remote "$ROOT/unseeded.git" 2>&1); expect "$?" 0 "the adopt that came too early now works when run again"
expect "$(cat .claude/CLAUDE.md 2>&1)" "hank global" "and the files arrive"

step "Settings (README: Settings)"
as alice
out=$(devsync config set skip-project-dirs archive,scratch 2>&1); expect "$?" 0 "config set skip-project-dirs"
mkdir -p $ROOT/home/alice/work; out=$(devsync config set root $ROOT/home/alice/work 2>&1); expect "$?" 0 "config set root"
has "$out" "project-key-prefix -> $(key "$ROOT/home/alice/work")" "the derived prefix moved with the root"
devsync config set root $ROOT/home/alice/code >/dev/null 2>&1

step "Monitor (docs/AUTOMATION.md) -- the templates ship inside the install"
as alice
out=$(devsync monitor install 2>&1); expect "$?" 0 "monitor install"
has "$(git --git-dir=$STORE show main:.github/heartbeat_check.py 2>&1 | head -1)" "python3" "the check script reached the store"
has "$(git --git-dir=$STORE show main:.github/workflows/sync-heartbeat.yml 2>&1 | head -1)" "devsync heartbeat" "the workflow reached the store"
chk=$(cd "$HOME/code/devsync-store" && python3 .github/heartbeat_check.py 2>&1)
has "$chk" "h ago (threshold 72h)" "the check reports a watched machine"

step "Unattended run (docs/AUTOMATION.md) -- the wrapper finds a pipx install"
as alice
if [ -f "$SPEC/scripts/devsync-sync.sh" ]; then
  out=$(DEVSYNC_PY=/nonexistent DEVSYNC_LOG="$HOME/wrapper.log" bash "$SPEC/scripts/devsync-sync.sh" manual 2>&1); expect "$?" 0 "the Linux wrapper runs with only a pipx install"
  has "$(cat "$HOME/wrapper.log" 2>&1)" "OK" "and logs OK"
else
  ok "wrapper step skipped: installed from a URL, no checkout at hand"
fi
out=$(devsync config set sync-repo-files on 2>&1); expect "$?" 2 "turning secret sync on in an unencrypted store is refused"
out=$(devsync restore claude/global/NOPE.md 2>&1); lacks "$out" "Traceback" "a bad restore is a message, not a traceback"
as frank      # a second machine that runs the first-machine command by mistake
mkdir -p code .claude; printf "frank global
" > .claude/CLAUDE.md
out=$(devsync init --seed --remote "$STORE" 2>&1); expect "$?" 1 "seeding a store that already has files is refused"
expect "$(cat .claude/CLAUDE.md)" "frank global" "and that machine is left untouched"

step "Secrets (README: Secrets) -- encrypted store"
as carol
mkdir -p code/app .claude
printf 'carol global\n' > .claude/CLAUDE.md
(cd code/app && git init -q --initial-branch=main && printf '.env\n' > .gitignore && printf 'TOKEN=abc123secret\n' > .env)
out=$(devsync init --seed --sync-repo-files --remote "$STORE2" 2>&1); rc=$?
expect "$rc" 1 "without the crypto package it refuses rather than upload plain text"
has "$out" "cryptography" "and says what to install"
printf '%s\n' "$out" | tail -2
out=$(pipx inject ai-devsync cryptography 2>&1)
expect "$?" 0 "the README's install-encryption command works"
export DEVSYNC_PASSPHRASE='correct horse battery staple'
out=$(devsync init --seed --sync-repo-files --remote "$STORE2" 2>&1); rc=$?
expect "$rc" 0 "init --seed --sync-repo-files with a passphrase"
blob=$(git --git-dir=$STORE2 show main:repos/app/.env 2>&1 | head -c 12); expect "$blob" "DEVSYNC-ENC1" "the .env in the store is encrypted"
leak=$(git --git-dir=$STORE2 grep -a -l abc123secret $(git --git-dir=$STORE2 rev-list --all) 2>/dev/null | wc -l); expect "$leak" 0 "the secret appears nowhere in the store history"
as dave
mkdir -p code/app .claude; (cd code/app && git init -q --initial-branch=main && printf '.env\n' > .gitignore)
export DEVSYNC_PASSPHRASE='wrong'; out=$(devsync adopt --remote "$STORE2" 2>&1); expect "$?" 1 "a wrong passphrase is refused"
export DEVSYNC_PASSPHRASE='correct horse battery staple'; out=$(devsync adopt --remote "$STORE2" 2>&1); expect "$?" 0 "adopt with the passphrase"
expect "$(cat code/app/.env 2>&1)" "TOKEN=abc123secret" "dave gets the plain .env"
unset DEVSYNC_PASSPHRASE

step "A user with no git identity configured"
export HOME=$ROOT/home/erin; mkdir -p "$HOME/code" "$HOME/.claude"; cd "$HOME"; printf 'erin\n' > .claude/CLAUDE.md
git init -q --bare --initial-branch=main $ROOT/store3.git
out=$(devsync init --seed --remote $ROOT/store3.git 2>&1); rc=$?
expect "$rc" 0 "init works without a git identity"
[ $rc -ne 0 ] && printf '%s\n' "$out" | tail -3
has "$(git --git-dir=$ROOT/store3.git log -1 --format=%an main 2>&1)" "devsync" "the commit carries a neutral author"

printf '\n==== %s step(s) failed ====\n' "$fails"
exit $fails
