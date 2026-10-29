import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import creds_crypto as cc  # noqa: E402

PASS = b"correct horse battery staple"


class CredsCryptoTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        blob = cc.seal(b"secret bundle", PASS)
        self.assertTrue(blob.startswith(cc.MAGIC))
        self.assertEqual(cc.open_(blob, PASS), b"secret bundle")

    def test_wrong_passphrase_fails_the_mac(self) -> None:
        with self.assertRaisesRegex(cc.CredsError, "integrity"):
            cc.open_(cc.seal(b"x", PASS), b"wrong")

    def test_any_modified_byte_is_rejected_before_decryption(self) -> None:
        blob = bytearray(cc.seal(b"secret bundle" * 10, PASS))
        for pos in (len(cc.MAGIC), len(blob) - 1, len(blob) // 2):
            bad = bytearray(blob)
            bad[pos] ^= 1
            with mock.patch.object(cc, "_openssl", wraps=cc._openssl) as spy:
                with self.assertRaises(cc.CredsError):
                    cc.open_(bytes(bad), PASS)
                self.assertFalse(any("-d" in c.args[0] for c in spy.call_args_list))

    def test_truncation_is_rejected(self) -> None:
        with self.assertRaises(cc.CredsError):
            cc.open_(cc.seal(b"secret bundle", PASS)[:-16], PASS)

    def test_legacy_files_need_explicit_opt_in(self) -> None:
        legacy = cc._openssl(["-aes-256-cbc", "-salt", "-pbkdf2", "-iter", str(cc.LEGACY_ITER)], PASS, b"old")
        with self.assertRaisesRegex(cc.CredsError, "--legacy"):
            cc.open_(legacy, PASS)
        self.assertEqual(cc.open_(legacy, PASS, legacy=True), b"old")

    def test_passphrase_never_in_argv(self) -> None:
        with mock.patch.object(cc.subprocess, "run", wraps=subprocess.run) as run:
            cc.seal(b"x", PASS)
        for call in run.call_args_list:
            self.assertNotIn(PASS.decode(), " ".join(call.args[0]))


if __name__ == "__main__":
    unittest.main()
