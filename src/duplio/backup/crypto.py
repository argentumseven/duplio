"""Client-side encryption for backup repositories.

Everything written to an encrypted repository — data chunks, file lists, snapshot
summaries — is encrypted and authenticated *before* it leaves this computer, so a
NAS, a cloud-synced folder or a stolen USB drive reveals nothing but sizes and times.

* **AES-256-GCM** for every object. The object's identity (its id, or the snapshot id)
  is bound in as associated data, so an attacker cannot swap one encrypted file for another.
* **Keyed object ids** (HMAC-SHA256). With plain content hashes, anyone holding the
  repository could test whether a known file is in it; with a secret key they cannot.
  Identical chunks still get identical ids, so deduplication works unchanged.
* **A random master key** (32 bytes of encryption key + 32 of id key) is generated once.
  The password does not encrypt data directly: it derives a key (scrypt) that only
  *wraps* the master key. Changing the password rewrites one small block, not the repository.
* The password is NFC-normalised, so the same password typed on a different keyboard
  layout or operating system still opens the repository.

The encryption block lives in ``duplio-repo.json``. **Lose the password and the data is gone;
there is no recovery.** Duplio says so when the repository is created.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import unicodedata
from typing import Any

from duplio.errors import RepositoryError

try:  # optional dependency: `pip install 'duplio[encrypt]'`
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when the package is missing
    AVAILABLE = False

MAGIC = b"DPE1"
NONCE_LEN = 12
SCHEME = "aes-256-gcm"
DEFAULT_SCRYPT_N = 2**15  # about 32 MiB of memory, ~0.1 s: slow enough to hamper guessing, quick enough to type
MIN_PASSWORD_LEN = 8


class PasswordRequired(RepositoryError):
    """The repository is encrypted and no password was supplied."""


class WrongPassword(RepositoryError):
    """The password does not unlock the repository."""


def require_library() -> None:
    if not AVAILABLE:
        raise RepositoryError("encryption needs the 'cryptography' package: pip install 'duplio[encrypt]'")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _password_bytes(password: str) -> bytes:
    return unicodedata.normalize("NFC", password).encode("utf-8")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    try:
        return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(_password_bytes(password))
    except (MemoryError, ValueError, OverflowError) as exc:
        raise RepositoryError(f"the key could not be derived with the repository's settings ({exc})") from None


def _wrap(master: bytes, password: str, repo_id: str, *, n: int) -> dict[str, Any]:
    salt = os.urandom(16)
    kek = _derive(password, salt, n, 8, 1)
    nonce = os.urandom(NONCE_LEN)
    sealed = AESGCM(kek).encrypt(nonce, master, b"duplio-key:" + repo_id.encode())
    return {
        "scheme": SCHEME,
        "kdf": {"name": "scrypt", "salt": _b64(salt), "n": n, "r": 8, "p": 1},
        "wrapped_key": _b64(nonce + sealed),
    }


class RepoCrypto:
    """Seals and opens repository objects; holds the master key in memory only."""

    def __init__(self, master: bytes) -> None:
        if len(master) != 64:
            raise RepositoryError("the repository key has an unexpected length")
        self._master = master
        self._aead = AESGCM(master[:32])
        self._id_key = master[32:]

    # ---- creating and unlocking
    @classmethod
    def create(cls, password: str, repo_id: str, *, scrypt_n: int = DEFAULT_SCRYPT_N) -> tuple[RepoCrypto, dict[str, Any]]:
        require_library()
        if len(password) < MIN_PASSWORD_LEN:
            raise RepositoryError(f"choose a password of at least {MIN_PASSWORD_LEN} characters")
        master = os.urandom(64)
        return cls(master), _wrap(master, password, repo_id, n=scrypt_n)

    @classmethod
    def unlock(cls, password: str, block: dict[str, Any], repo_id: str) -> RepoCrypto:
        require_library()
        if block.get("scheme") != SCHEME or block.get("kdf", {}).get("name") != "scrypt":
            raise RepositoryError("this repository uses an encryption scheme this Duplio does not know")
        kdf = block["kdf"]
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
        if n > 2**22 or r > 32 or p > 16 or 128 * r * n > 2**30:  # memory is about 128 * r * n bytes
            raise RepositoryError("the repository asks for an unreasonable key-derivation cost; refusing")
        kek = _derive(password, _unb64(kdf["salt"]), n, r, p)
        raw = _unb64(block["wrapped_key"])
        try:
            master = AESGCM(kek).decrypt(raw[:NONCE_LEN], raw[NONCE_LEN:], b"duplio-key:" + repo_id.encode())
        except InvalidTag:
            raise WrongPassword("wrong password for this repository") from None
        return cls(master)

    def rewrap(self, new_password: str, repo_id: str, *, scrypt_n: int = DEFAULT_SCRYPT_N) -> dict[str, Any]:
        """A new encryption block for the *same* master key (used to change the password)."""
        if len(new_password) < MIN_PASSWORD_LEN:
            raise RepositoryError(f"choose a password of at least {MIN_PASSWORD_LEN} characters")
        return _wrap(self._master, new_password, repo_id, n=scrypt_n)

    # ---- objects
    def object_id(self, data: bytes | memoryview) -> str:
        return hmac.new(self._id_key, data, hashlib.sha256).hexdigest()

    def seal(self, plain: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_LEN)
        return MAGIC + nonce + self._aead.encrypt(nonce, plain, aad)

    def open(self, blob: bytes, aad: bytes) -> bytes:
        if blob[:4] != MAGIC or len(blob) < 4 + NONCE_LEN + 16:
            raise RepositoryError("an encrypted repository holds an unencrypted or damaged object")
        try:
            return self._aead.decrypt(blob[4 : 4 + NONCE_LEN], blob[4 + NONCE_LEN :], aad)
        except InvalidTag:
            raise RepositoryError("object failed authentication: it is corrupt or has been tampered with") from None
