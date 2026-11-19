"""Merge-rule tests: category rules and the reconcile matrix."""
from devsync.merge import (
    category_for,
    merge_env,
    merge_structured_json,
    merge_text,
    reconcile,
)

MANIFEST = {
    "categoryByExt": {
        ".md": "text", ".txt": "text",
        ".env": "env", ".ini": "env", ".properties": "env",
        ".json": "structured", ".yaml": "structured", ".yml": "structured",
        ".toml": "structured",
        ".png": "binary", ".jpg": "binary", ".pfx": "binary",
        ".sqlite": "binary", ".pem": "binary", ".key": "binary",
    },
}


# ── category detection ──────────────────────────────────────────

def test_category_by_extension():
    assert category_for("claude/memory/CascadeProjects/MEMORY.md", MANIFEST) == "text"
    assert category_for("claude/global/settings.json", MANIFEST) == "structured"
    assert category_for("repos/foo/logo.png", MANIFEST) == "binary"


def test_category_env_basename_variants():
    # .env, .env.local, .env.production — ext-map alone misses these
    assert category_for("repos/foo/.env", MANIFEST) == "env"
    assert category_for("repos/foo/.env.local", MANIFEST) == "env"
    assert category_for("repos/foo/.env.production", MANIFEST) == "env"


def test_category_unknown_fallback():
    assert category_for("repos/foo/data.xyz", MANIFEST) == "unknown"


# ── text 3-way (the memory-file core requirement) ───────────────

def test_text_additive_union():
    base = b"- fact 1\n- fact 2\n"
    ours = b"- fact 1\n- fact 2\n- fact B\n"
    theirs = b"- fact 1\n- fact 2\n- fact A\n"
    merged, conflict = merge_text(base, ours, theirs)
    assert not conflict
    assert b"fact A" in merged and b"fact B" in merged
    assert b"fact 1" in merged and b"fact 2" in merged


def test_text_same_line_conflict_keeps_both():
    base = b"line one\nline two\n"
    ours = b"line one CHANGED-B\nline two\n"
    theirs = b"line one CHANGED-A\nline two\n"
    merged, conflict = merge_text(base, ours, theirs)
    assert conflict
    assert b"CHANGED-A" in merged and b"CHANGED-B" in merged  # nothing lost


def test_text_one_side_unchanged_takes_other():
    base = b"a\nb\n"
    ours = b"a\nb\n"
    theirs = b"a\nb\nc\n"
    merged, conflict = merge_text(base, ours, theirs)
    assert not conflict
    assert merged == b"a\nb\nc\n"


# ── env key-aware merge ─────────────────────────────────────────

def test_env_new_keys_from_both_sides_survive():
    base = b"A=1\nB=2\n"
    ours = b"A=1\nB=2\nC=3\n"
    theirs = b"A=1\nB=2\nD=4\n"
    merged, conflict, _ = merge_env(base, ours, theirs, ours_newer=True)
    assert not conflict
    text = merged.decode()
    assert "A=1" in text and "B=2" in text and "C=3" in text and "D=4" in text


def test_env_same_key_changed_both_sides_newer_wins_loser_recorded():
    base = b"A=1\n"
    ours = b"A=ours\n"
    theirs = b"A=theirs\n"
    merged, conflict, loser = merge_env(base, ours, theirs, ours_newer=True)
    assert conflict
    assert b"A=ours" in merged
    assert "theirs" in loser
    merged2, conflict2, loser2 = merge_env(base, ours, theirs, ours_newer=False)
    assert b"A=theirs" in merged2
    assert "ours" in loser2


def test_env_key_deleted_one_side_survives_union():
    # anti-wipe bias: union of keys, deletion never inferred
    base = b"A=1\nB=2\n"
    ours = b"A=1\n"          # B deleted locally
    theirs = b"A=1\nB=2\n"
    merged, conflict, _ = merge_env(base, ours, theirs, ours_newer=True)
    assert b"B=2" in merged
    assert not conflict


def test_env_value_changed_one_side_only():
    base = b"A=1\nB=2\n"
    ours = b"A=1\nB=2\n"
    theirs = b"A=99\nB=2\n"
    merged, conflict, _ = merge_env(base, ours, theirs, ours_newer=True)
    assert not conflict
    assert b"A=99" in merged


def test_env_unparseable_falls_back_to_line_merge():
    base = b"not really env\n"
    ours = b"not really env\nours line\n"
    theirs = b"not really env\ntheirs line\n"
    merged, conflict, _ = merge_env(base, ours, theirs, ours_newer=True)
    assert b"ours line" in merged and b"theirs line" in merged


# ── structured (JSON) ───────────────────────────────────────────

def test_json_formatting_only_diff_is_not_a_conflict():
    base = b'{"a": 1, "b": 2}'
    ours = b'{\n  "b": 2,\n  "a": 1\n}\n'      # reordered/reformatted only
    theirs = b'{"a": 1, "b": 2, "c": 3}'        # real addition
    merged, conflict = merge_structured_json(base, ours, theirs)
    assert not conflict
    import json
    data = json.loads(merged)
    assert data == {"a": 1, "b": 2, "c": 3}


def test_json_invalid_merge_keeps_local():
    base = b'{"a": 1}'
    ours = b'{"a": 2}'
    theirs = b'{"a": 3}'
    merged, conflict = merge_structured_json(base, ours, theirs)
    assert conflict
    import json
    assert json.loads(merged) == {"a": 2}  # local kept, parseable


# ── CRLF/LF round-trip (Windows live files vs LF-normalized store) ──

def test_text_crlf_vs_lf_identical_content_is_noop():
    # First-sync scenario on Windows: live file CRLF, repo clone LF, same content.
    # Must be treated as equal — no churn, no conflict.
    lf = b"- fact 1\n- fact 2\n"
    crlf = b"- fact 1\r\n- fact 2\r\n"
    r = reconcile("text", lf, crlf, lf, ours_newer=True)
    assert not r.conflict
    assert r.merged.replace(b"\r\n", b"\n") == lf


def test_text_crlf_live_with_real_edits_merges_cleanly():
    base = b"- fact 1\n"
    live_crlf = b"- fact 1\r\n- fact B\r\n"      # CRLF + a new line
    repo_lf = b"- fact 1\n- fact A\n"            # LF + a different new line
    r = reconcile("text", base, live_crlf, repo_lf, ours_newer=True)
    assert not r.conflict
    norm = r.merged.replace(b"\r\n", b"\n")
    assert b"- fact A\n" in norm and b"- fact B\n" in norm


def test_env_crlf_vs_lf_identical_is_noop():
    lf = b"A=1\nB=2\n"
    crlf = b"A=1\r\nB=2\r\n"
    r = reconcile("env", lf, crlf, lf, ours_newer=True)
    assert not r.conflict
    assert r.merged.replace(b"\r\n", b"\n") == lf


def test_structured_crlf_vs_lf_identical_is_noop():
    lf = b'{\n  "a": 1\n}\n'
    crlf = b'{\r\n  "a": 1\r\n}\r\n'
    r = reconcile("structured", lf, crlf, lf, ours_newer=True)
    assert not r.conflict


def test_binary_crlf_not_normalized():
    # binary category must NOT be line-normalized — bytes differ, newer wins
    r = reconcile("binary", b"x\n", b"x\r\n", b"x\n", ours_newer=True)
    assert r.merged == b"x\r\n"


# ── reconcile matrix ──

def test_reconcile_identical_noop():
    r = reconcile("text", b"x\n", b"x\n", b"x\n", ours_newer=True)
    assert r.merged == b"x\n" and not r.conflict


def test_reconcile_live_only_file_is_added():
    r = reconcile("text", None, b"new\n", None, ours_newer=True)
    assert r.merged == b"new\n" and not r.conflict


def test_reconcile_repo_only_file_is_adopted():
    r = reconcile("text", None, None, b"incoming\n", ours_newer=True)
    assert r.merged == b"incoming\n" and not r.conflict


def test_reconcile_live_deleted_without_tombstone_comes_back():
    # deletion is never inferred from absence
    r = reconcile("text", b"keep\n", None, b"keep\n", ours_newer=True)
    assert r.merged == b"keep\n"


def test_reconcile_repo_absent_live_kept():
    r = reconcile("text", b"keep\n", b"keep\n", None, ours_newer=True)
    assert r.merged == b"keep\n"


def test_reconcile_binary_newer_wins_loser_preserved():
    r = reconcile("binary", b"old", b"local-new", b"remote-new", ours_newer=False)
    assert r.merged == b"remote-new"
    assert r.loser == b"local-new"   # goes to trash, never dropped


def test_reconcile_binary_only_one_side_changed():
    r = reconcile("binary", b"old", b"old", b"remote-new", ours_newer=True)
    assert r.merged == b"remote-new"
    assert r.loser is None


def test_reconcile_all_absent():
    r = reconcile("text", None, None, None, ours_newer=True)
    assert r.merged is None
