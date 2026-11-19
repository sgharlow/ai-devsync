"""Client-side encryption of the store (0.2.0).

The claims under test:
  * nothing secret reaches the remote in plain text once encryption is on;
  * every machine with the passphrase still gets byte-identical live files and
    real 3-way merges -- including the git-level conflict path;
  * a machine WITHOUT the key, or running a build too old for the store, stops
    before it writes a single live file;
  * a store that never turned encryption on needs neither a key nor the
    `cryptography` package.
"""
import json
import subprocess
from pathlib import Path

import pytest

from devsync import crypto
from devsync.cli import main
from devsync.engine import Engine
from devsync.paths import Machine

pytest.importorskip("cryptography", reason="encryption tests need the cryptography package")

PREFIX_A = "C--Users-A-code"
PREFIX_B = "C--Users-B-code"
PHRASE = "correct horse battery staple"
SECRET = "API_TOKEN=s3cr3t-value-123\n"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _remote_bytes(remote, path):
    r = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{path}"],
                       capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _remote_head(remote):
    return subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()


def _remote_has_plaintext(remote, needle: bytes) -> bool:
    """Search every blob in every commit, not just the tip."""
    revs = subprocess.run(["git", "--git-dir", str(remote), "rev-list", "--all"],
                          capture_output=True, text=True).stdout.split()
    for rev in revs:
        g = subprocess.run(["git", "--git-dir", str(remote), "grep", "-a", "-l",
                            needle.decode(), rev], capture_output=True, text=True)
        if g.stdout.strip():
            return True
    return False


def _use(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DEVSYNC_HOME", str(home / ".devsync"))


def _make_pc(home, prefix, with_project=True, env=SECRET):
    (home / "code").mkdir(parents=True)
    mem = home / ".claude" / "projects" / prefix / "memory"
    mem.mkdir(parents=True)
    if with_project:
        proj = home / "code" / "app"
        proj.mkdir()
        _git(proj, "init", "--initial-branch=main")
        (proj / ".gitignore").write_text(".env\n")
        (proj / ".env").write_text(env)
    return home


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var in ("DEVSYNC_ROOT", "DEVSYNC_MEMORY_KEY_ROOT", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEVSYNC_PASSPHRASE", PHRASE)
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@devsync.invalid"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@devsync.invalid")):
        monkeypatch.setenv(k, v)
    a = _make_pc(tmp_path / "a", PREFIX_A)
    (a / ".claude" / "CLAUDE.md").write_text("global notes\n")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _use(monkeypatch, a)
    return a, remote, tmp_path


def _seed(a, remote, *extra):
    return main(["init", "--seed", *extra, "--remote", str(remote), "--root", str(a / "code"),
                 "--claude-home", str(a / ".claude"), "--hostname", "PC-A",
                 "--project-key-prefix", PREFIX_A])


def _adopt(b, remote, *extra):
    return main(["adopt", *extra, "--remote", str(remote), "--root", str(b / "code"),
                 "--claude-home", str(b / ".claude"), "--hostname", "PC-B",
                 "--project-key-prefix", PREFIX_B])


def _engine(home, prefix, name):
    data = json.loads((home / ".devsync" / "machine.json").read_text())
    m = Machine(hostname=name, cascade_root=Path(data["cascadeRoot"]),
                claude_home=Path(data["claudeHome"]), project_key_prefix=prefix)
    return Engine(machine=m, store_dir=Path(data["storeDir"]), state_dir=home / ".devsync")


# ── the primitive ────────────────────────────────────────────────────────────

def test_round_trip_including_an_empty_file():
    cfg, key = crypto.new_config(PHRASE)
    for data in (b"", b"A=1\n", bytes(range(256)) * 40):
        blob = crypto.encrypt(key, "repos/app/.env", data)
        assert crypto.is_encrypted(blob) and data not in (blob,) and blob != data
        assert crypto.decrypt(key, "repos/app/.env", blob) == data


def test_same_content_same_path_gives_the_same_bytes():
    cfg, key = crypto.new_config(PHRASE)
    assert crypto.encrypt(key, "repos/a/.env", b"X=1\n") == crypto.encrypt(key, "repos/a/.env", b"X=1\n")
    assert crypto.encrypt(key, "repos/a/.env", b"X=1\n") != crypto.encrypt(key, "repos/b/.env", b"X=1\n")


def test_a_tampered_or_moved_file_is_rejected_not_decoded():
    cfg, key = crypto.new_config(PHRASE)
    blob = crypto.encrypt(key, "repos/a/.env", b"X=1\n")
    flipped = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(crypto.EncryptionError):
        crypto.decrypt(key, "repos/a/.env", flipped)
    with pytest.raises(crypto.EncryptionError):
        crypto.decrypt(key, "repos/other/.env", blob)


def test_the_wrong_passphrase_is_detected_without_decrypting_anything():
    cfg, key = crypto.new_config(PHRASE)
    assert crypto.key_matches(key, cfg)
    assert not crypto.key_matches(crypto.derive_key("not the passphrase", cfg), cfg)
    assert crypto.derive_key(PHRASE, cfg) == key


# ── a new encrypted store ────────────────────────────────────────────────────

def test_secret_sync_is_encrypted_by_default(world, capsys):
    a, remote, _ = world
    assert _seed(a, remote, "--sync-repo-files") == 0
    blob = _remote_bytes(remote, "repos/app/.env")
    assert blob is not None and blob.startswith(crypto.MAGIC)
    assert b"s3cr3t" not in blob
    assert not _remote_has_plaintext(remote, b"s3cr3t-value-123")
    manifest = json.loads(_remote_bytes(remote, "manifest.json"))
    assert manifest["encryption"]["paths"] == ["repos/**"]
    assert manifest["requires"] == "0.2.0"
    assert PHRASE not in json.dumps(manifest)
    assert "encrypted" in capsys.readouterr().out
    # the live file and the merge base are untouched plaintext
    assert (a / "code" / "app" / ".env").read_text() == SECRET
    assert (a / ".devsync" / "base" / "repos" / "app" / ".env").read_text() == SECRET


def test_config_and_memory_stay_readable_in_the_store(world):
    a, remote, _ = world
    assert _seed(a, remote, "--sync-repo-files") == 0
    assert _remote_bytes(remote, "claude/global/CLAUDE.md") == b"global notes\n"


def test_an_unchanged_encrypted_store_produces_no_new_commit(world):
    a, remote, _ = world
    assert _seed(a, remote, "--sync-repo-files") == 0
    head = _remote_head(remote)
    assert main(["sync"]) == 0
    assert main(["sync"]) == 0
    assert _remote_head(remote) == head


def test_a_second_pc_with_the_passphrase_gets_the_plain_file(world, monkeypatch):
    a, remote, tmp = world
    assert _seed(a, remote, "--sync-repo-files") == 0
    b = _make_pc(tmp / "b", PREFIX_B, with_project=False)
    proj = b / "code" / "app"
    proj.mkdir()
    _git(proj, "init", "--initial-branch=main")
    (proj / ".gitignore").write_text(".env\n")
    _use(monkeypatch, b)
    assert _adopt(b, remote) == 0
    assert (proj / ".env").read_text() == SECRET
    assert (b / ".devsync" / "key").is_file()


def test_a_second_pc_with_the_wrong_passphrase_changes_nothing(world, monkeypatch, capsys):
    a, remote, tmp = world
    assert _seed(a, remote, "--sync-repo-files") == 0
    head = _remote_head(remote)
    b = _make_pc(tmp / "b", PREFIX_B, env="LOCAL=only\n")
    _use(monkeypatch, b)
    monkeypatch.setenv("DEVSYNC_PASSPHRASE", "wrong")
    assert _adopt(b, remote) == 1
    assert "does not open this store" in capsys.readouterr().err
    assert (b / "code" / "app" / ".env").read_text() == "LOCAL=only\n"
    assert not (b / ".devsync" / "machine.json").exists()
    assert not (b / ".devsync" / "key").exists()
    assert _remote_head(remote) == head


def test_two_pcs_merge_their_env_edits_through_the_encrypted_store(world, monkeypatch):
    a, remote, tmp = world
    assert _seed(a, remote, "--sync-repo-files") == 0
    b = _make_pc(tmp / "b", PREFIX_B, with_project=False)
    proj_b = b / "code" / "app"
    proj_b.mkdir()
    _git(proj_b, "init", "--initial-branch=main")
    (proj_b / ".gitignore").write_text(".env\n")
    _use(monkeypatch, b)
    assert _adopt(b, remote) == 0

    (proj_b / ".env").write_text(SECRET + "FROM_B=2\n")
    assert main(["sync"]) == 0
    _use(monkeypatch, a)
    (a / "code" / "app" / ".env").write_text(SECRET + "FROM_A=1\n")
    assert main(["sync"]) == 0

    merged = (a / "code" / "app" / ".env").read_text()
    assert "FROM_A=1" in merged and "FROM_B=2" in merged and "API_TOKEN=s3cr3t-value-123" in merged
    assert _remote_bytes(remote, "repos/app/.env").startswith(crypto.MAGIC)
    assert not _remote_has_plaintext(remote, b"FROM_B=2")


def test_a_git_level_conflict_on_an_encrypted_file_is_merged_in_plaintext(world, monkeypatch):
    """Both PCs commit a different edit before either sees the other's. Git cannot
    merge two ciphertexts, so the pull stops; the engine has to decode all three
    sides, merge, and store the result encrypted again."""
    a, remote, tmp = world
    assert _seed(a, remote, "--sync-repo-files") == 0
    b = _make_pc(tmp / "b", PREFIX_B, with_project=False)
    proj_b = b / "code" / "app"
    proj_b.mkdir()
    _git(proj_b, "init", "--initial-branch=main")
    (proj_b / ".gitignore").write_text(".env\n")
    _use(monkeypatch, b)
    assert _adopt(b, remote) == 0

    (proj_b / ".env").write_text(SECRET + "FROM_B=2\n")
    held = _engine(b, PREFIX_B, "PC-B").sync(push=False)       # committed, not pushed
    assert not held.pushed

    _use(monkeypatch, a)
    (a / "code" / "app" / ".env").write_text(SECRET + "FROM_A=1\n")
    assert main(["sync"]) == 0

    _use(monkeypatch, b)
    assert main(["sync"]) == 0
    merged = (proj_b / ".env").read_text()
    assert "FROM_A=1" in merged and "FROM_B=2" in merged
    blob = _remote_bytes(remote, "repos/app/.env")
    assert blob.startswith(crypto.MAGIC)
    stored = crypto.decrypt(crypto.load_key(b / ".devsync"), "repos/app/.env", blob).decode()
    assert "FROM_A=1" in stored and "FROM_B=2" in stored
    assert "<<<<<<<" not in stored


# ── turning it on for a store that already exists ────────────────────────────

def test_encrypt_refuses_until_told_every_pc_is_upgraded(world, capsys):
    a, remote, _ = world
    assert _seed(a, remote, "--sync-repo-files", "--no-encrypt") == 0
    head = _remote_head(remote)
    assert main(["encrypt"]) == 2
    assert "0.2.0" in capsys.readouterr().err
    assert _remote_head(remote) == head
    assert _remote_bytes(remote, "repos/app/.env") == SECRET.encode()


def test_encrypt_converts_an_existing_plain_store(world):
    a, remote, _ = world
    assert _seed(a, remote, "--sync-repo-files", "--no-encrypt") == 0
    assert main(["encrypt", "--all-machines-upgraded"]) == 0
    blob = _remote_bytes(remote, "repos/app/.env")
    assert blob.startswith(crypto.MAGIC) and b"s3cr3t" not in blob
    assert (a / "code" / "app" / ".env").read_text() == SECRET
    assert json.loads(_remote_bytes(remote, "manifest.json"))["requires"] == "0.2.0"


def test_a_pc_without_the_key_stops_before_touching_live_files(world, monkeypatch, capsys):
    a, remote, tmp = world
    assert _seed(a, remote, "--sync-repo-files", "--no-encrypt") == 0
    b = _make_pc(tmp / "b", PREFIX_B, with_project=False)
    proj_b = b / "code" / "app"
    proj_b.mkdir()
    _git(proj_b, "init", "--initial-branch=main")
    (proj_b / ".gitignore").write_text(".env\n")
    _use(monkeypatch, b)
    assert _adopt(b, remote) == 0
    assert (proj_b / ".env").read_text() == SECRET

    _use(monkeypatch, a)
    (a / "code" / "app" / ".env").write_text(SECRET + "NEW=1\n")
    assert main(["encrypt", "--all-machines-upgraded"]) == 0

    _use(monkeypatch, b)
    monkeypatch.delenv("DEVSYNC_PASSPHRASE")
    capsys.readouterr()
    assert main(["sync"]) == 1
    assert "devsync unlock" in capsys.readouterr().err
    assert (proj_b / ".env").read_text() == SECRET          # not ciphertext, not changed

    monkeypatch.setenv("DEVSYNC_PASSPHRASE", PHRASE)
    assert main(["unlock"]) == 0
    assert main(["sync"]) == 0
    assert (proj_b / ".env").read_text() == SECRET + "NEW=1\n"


# ── gates ────────────────────────────────────────────────────────────────────

def test_a_store_that_requires_a_newer_build_is_refused_before_any_write(world):
    a, remote, _ = world
    assert _seed(a, remote) == 0
    store = a / "code" / "devsync-store"
    manifest = json.loads((store / "manifest.json").read_text())
    manifest["requires"] = "9.0.0"
    (store / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (a / ".claude" / "CLAUDE.md").write_text("edited after the gate went up\n")
    head = _remote_head(remote)
    with pytest.raises(RuntimeError, match="requires devsync >= 9.0.0"):
        _engine(a, PREFIX_A, "PC-A").sync()
    assert _remote_head(remote) == head


def test_seeding_with_secrets_and_no_crypto_package_points_at_the_way_out(world, monkeypatch, capsys):
    a, remote, _ = world
    monkeypatch.setattr(crypto, "available", lambda: False)
    assert _seed(a, remote, "--sync-repo-files") == 1
    err = capsys.readouterr().err
    assert "pip install cryptography" in err and "--no-encrypt" in err
    assert not (a / ".devsync" / "machine.json").exists()


def test_seeding_encrypted_with_no_way_to_ask_for_a_passphrase_fails_cleanly(world, monkeypatch, capsys):
    a, remote, _ = world
    monkeypatch.delenv("DEVSYNC_PASSPHRASE")
    monkeypatch.setattr("sys.stdin", None)
    assert _seed(a, remote, "--sync-repo-files") == 1
    assert "DEVSYNC_PASSPHRASE" in capsys.readouterr().err
    assert not (a / ".devsync" / "machine.json").exists()


def test_an_unencrypted_store_needs_neither_a_key_nor_the_crypto_package(world, monkeypatch):
    a, remote, _ = world
    monkeypatch.delenv("DEVSYNC_PASSPHRASE")
    monkeypatch.setattr(crypto, "available", lambda: False)
    assert _seed(a, remote, "--sync-repo-files", "--no-encrypt") == 0
    assert main(["sync"]) == 0
    assert _remote_bytes(remote, "repos/app/.env") == SECRET.encode()
    assert not (a / ".devsync" / "key").exists()
    manifest = json.loads(_remote_bytes(remote, "manifest.json"))
    assert "encryption" not in manifest and "requires" not in manifest
