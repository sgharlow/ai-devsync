"""Client-side encryption of store files (optional; needs the `cryptography` package).

What it protects: the CONTENT of chosen store paths (by default `repos/**`, the
gitignored files -- .env, keys, certs) from anyone who can read the store's Git
remote. Paths, file sizes and commit history stay visible. Live files and the
per-machine merge base stay plaintext on your own disk, as they always were.

How:
  * key   = scrypt(passphrase, salt)             -- stdlib hashlib, 64 bytes
  * file  = MAGIC + AES-SIV(key).encrypt(0x01 + plaintext, [canonical path])

AES-SIV is deterministic: the same plaintext at the same path always produces the
same bytes. That is the property a Git-backed store needs -- an unchanged file
must not look modified on every sync -- and it is what SIV mode exists for. The
cost is that an observer can tell when a file returns to earlier content. The
canonical path is bound in as associated data, so a ciphertext moved to another
path fails to decrypt instead of silently becoming that file.

The derived key is cached in ~/.devsync/key so unattended syncs work. It never
enters the store. The passphrase is never written anywhere.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from devsync.collect import _match_any

MAGIC = b"DEVSYNC-ENC1\n"
SCHEME = "aes-siv-v1"
DEFAULT_PATHS = ["repos/**"]
_CHECK_PLAINTEXT = b"devsync key check"
_CHECK_PATH = b"devsync/check"
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


class EncryptionError(RuntimeError):
    """The store is encrypted and this machine cannot read or write it yet."""


def available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESSIV  # noqa: F401
        return True
    except Exception:
        return False


def _siv(key: bytes):
    if not available():
        raise EncryptionError(
            "this store is encrypted and the `cryptography` package is not "
            "installed -- run: pip install cryptography   (installed with pipx? pipx inject ai-devsync cryptography)")
    from cryptography.hazmat.primitives.ciphers.aead import AESSIV
    return AESSIV(key)


def derive_key(passphrase: str, cfg: dict) -> bytes:
    salt = base64.b64decode(cfg["salt"])
    n, r, p = cfg.get("n", _SCRYPT["n"]), cfg.get("r", _SCRYPT["r"]), cfg.get("p", _SCRYPT["p"])
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=64, maxmem=256 * 1024 * 1024)


def new_config(passphrase: str, paths: list | None = None) -> tuple[dict, bytes]:
    """A fresh encryption block for manifest.json, and the key it implies."""
    cfg = {"scheme": SCHEME, "kdf": "scrypt",
           "salt": base64.b64encode(os.urandom(16)).decode("ascii"),
           **_SCRYPT, "paths": list(paths or DEFAULT_PATHS)}
    key = derive_key(passphrase, cfg)
    cfg["check"] = base64.b64encode(
        _siv(key).encrypt(_CHECK_PLAINTEXT, [_CHECK_PATH])).decode("ascii")
    return cfg, key


def key_matches(key: bytes, cfg: dict) -> bool:
    try:
        return _siv(key).decrypt(base64.b64decode(cfg["check"]),
                                 [_CHECK_PATH]) == _CHECK_PLAINTEXT
    except EncryptionError:
        raise
    except Exception:
        return False


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def applies(canon: str, cfg: dict | None) -> bool:
    return bool(cfg) and _match_any(canon, cfg.get("paths", DEFAULT_PATHS))


def encrypt(key: bytes, canon: str, data: bytes) -> bytes:
    # 0x01 prefix: AES-SIV rejects an empty message, and empty files are real.
    return MAGIC + _siv(key).encrypt(b"\x01" + data, [canon.encode("utf-8")])


def decrypt(key: bytes, canon: str, blob: bytes) -> bytes:
    try:
        plain = _siv(key).decrypt(blob[len(MAGIC):], [canon.encode("utf-8")])
    except EncryptionError:
        raise
    except Exception:
        raise EncryptionError(
            f"could not decrypt {canon}: wrong key, or the file was altered or "
            f"moved in the store") from None
    return plain[1:]


def key_file(state_dir: Path) -> Path:
    return Path(state_dir) / "key"


def load_key(state_dir: Path) -> bytes | None:
    f = key_file(state_dir)
    if not f.is_file():
        return None
    try:
        return base64.b64decode(f.read_text(encoding="ascii").strip())
    except Exception:
        return None


def save_key(state_dir: Path, key: bytes):
    f = key_file(state_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(base64.b64encode(key).decode("ascii") + "\n", encoding="ascii")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
