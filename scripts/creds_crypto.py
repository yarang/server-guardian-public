#!/usr/bin/env python3
"""Seal and open the credential bundle written by export-credentials.sh.

Format v2: b"SGCRED2\\n" + HMAC-SHA256 tag (32 bytes) + OpenSSL ciphertext
("Salted__" + salt + AES-256-CBC, PBKDF2-SHA256 600,000 iterations).

- The passphrase is read from stdin and handed to openssl on a pipe (-pass fd:N), so it never
  appears in a process argument list (ps).
- Encrypt-then-MAC: the tag covers the whole ciphertext and is checked in constant time before
  anything is decrypted, so a modified or truncated file is rejected instead of half-restored.
  The MAC key is PBKDF2(passphrase, "sg-creds-mac-v2" + salt), separate from the cipher key.
- Files from older versions (bare "Salted__", 100,000 iterations, no MAC) open only with --legacy.

Usage:
  creds_crypto.py seal <plain> <out>   < passphrase
  creds_crypto.py open <in> <plain> [--legacy]   < passphrase
"""

import hashlib
import hmac
import os
import subprocess
import sys

MAGIC = b"SGCRED2\n"
TAG_LEN = 32
ITER = 600_000
LEGACY_ITER = 100_000
MAC_CONTEXT = b"sg-creds-mac-v2"


class CredsError(Exception):
    pass


def _openssl(args, passphrase: bytes, data: bytes) -> bytes:
    """Runs openssl enc with the passphrase on a pipe instead of argv."""
    r, w = os.pipe()
    try:
        os.write(w, passphrase)
    finally:
        os.close(w)
    try:
        proc = subprocess.run(
            ["openssl", "enc", *args, "-pass", f"fd:{r}"],
            input=data,
            capture_output=True,
            pass_fds=(r,),
            check=False,
        )
    finally:
        os.close(r)
    if proc.returncode != 0:
        raise CredsError(proc.stderr.decode("utf-8", "replace").strip() or "openssl failed")
    return proc.stdout


def _mac_key(passphrase: bytes, ciphertext: bytes) -> bytes:
    if not ciphertext.startswith(b"Salted__") or len(ciphertext) < 32:
        raise CredsError("not an OpenSSL salted ciphertext")
    return hashlib.pbkdf2_hmac("sha256", passphrase, MAC_CONTEXT + ciphertext[8:16], ITER)


def seal(plaintext: bytes, passphrase: bytes) -> bytes:
    if not passphrase:
        raise CredsError("empty passphrase")
    ct = _openssl(["-aes-256-cbc", "-salt", "-pbkdf2", "-md", "sha256", "-iter", str(ITER)], passphrase, plaintext)
    tag = hmac.new(_mac_key(passphrase, ct), ct, hashlib.sha256).digest()
    return MAGIC + tag + ct


def open_(blob: bytes, passphrase: bytes, legacy: bool = False) -> bytes:
    if blob.startswith(MAGIC):
        tag, ct = blob[len(MAGIC) : len(MAGIC) + TAG_LEN], blob[len(MAGIC) + TAG_LEN :]
        want = hmac.new(_mac_key(passphrase, ct), ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, want):
            raise CredsError("integrity check failed: wrong passphrase or the file was modified")
        return _openssl(["-d", "-aes-256-cbc", "-pbkdf2", "-md", "sha256", "-iter", str(ITER)], passphrase, ct)
    if blob.startswith(b"Salted__"):
        if not legacy:
            raise CredsError("old format without an integrity check; re-run with --legacy if you trust this file")
        return _openssl(["-d", "-aes-256-cbc", "-pbkdf2", "-iter", str(LEGACY_ITER)], passphrase, blob)
    raise CredsError("not a Server Guardian credential bundle")


def _read_passphrase() -> bytes:
    data = sys.stdin.buffer.read()
    return data[:-1] if data.endswith(b"\n") else data


def main(argv) -> int:
    if len(argv) < 3 or argv[0] not in ("seal", "open"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd, src, dst = argv[0], argv[1], argv[2]
    try:
        with open(src, "rb") as f:
            data = f.read()
        out = seal(data, _read_passphrase()) if cmd == "seal" else open_(data, _read_passphrase(), "--legacy" in argv[3:])
    except (CredsError, OSError) as exc:
        print(f"creds_crypto: {exc}", file=sys.stderr)
        return 1
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
