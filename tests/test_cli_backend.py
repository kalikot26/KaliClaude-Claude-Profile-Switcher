from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gui.cli_backend import CliBackend, CliBackendError
from gui.desktop_backend import _sha256

CREDS = ".credentials.json"


class FakeSpawner:
    """Scriptable stand-in for a spawned login terminal.

    create_on/exit_on are 1-based poll counts: on the Nth poll() the spawner
    writes the live credentials file / reports the process as exited.
    """

    def __init__(self, live_creds: Path, *, create_on=None, exit_on=None, payload=b"NEWCLI"):
        self.live_creds = live_creds
        self.create_on = create_on
        self.exit_on = exit_on
        self.payload = payload
        self.polls = 0
        self.argv = None

    def __call__(self, argv):
        self.argv = argv
        return self

    def poll(self):
        self.polls += 1
        if self.create_on is not None and self.polls >= self.create_on:
            self.live_creds.parent.mkdir(parents=True, exist_ok=True)
            self.live_creds.write_bytes(self.payload)
        if self.exit_on is not None and self.polls >= self.exit_on:
            return 0
        return None


class CliBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # A clean environment so os.environ never masks the injected registry.
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def make(self, **kw) -> CliBackend:
        kw.setdefault("home", self.home)
        kw.setdefault("env_reader", lambda: {})
        kw.setdefault("which", lambda name: None)
        kw.setdefault("pair_interval", 0.0)
        kw.setdefault("pair_timeout", 1.0)
        return CliBackend(**kw)

    def cli_data(self, name: str) -> Path:
        return self.home / ".kalikot-claude-switcher" / "cli-data" / name

    def write_store(self, name, creds=b"STORE", account=None) -> Path:
        store = self.cli_data(name)
        store.mkdir(parents=True, exist_ok=True)
        (store / CREDS).write_bytes(creds)
        if account is not None:
            (store / "account.json").write_text(json.dumps(account))
        return store

    def write_live(self, data=b"LIVE") -> Path:
        path = self.home / ".claude" / CREDS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_claude_json(self, uuid="", email="") -> None:
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": uuid, "emailAddress": email}}))

    def assert_no_tmp(self) -> None:
        self.assertEqual([], list(self.home.rglob("*.tmp")))

    # ----- capture + install -----------------------------------------------

    def test_capture_and_install_are_byte_faithful_without_tmp_residue(self) -> None:
        backend = self.make()
        self.write_live(b"LIVE-A")
        self.write_store("beta", creds=b"BETA-CREDS")

        result = backend.switch_to("beta", "alpha")

        self.assertTrue(result.ok and result.installed and not result.needs_login)
        self.assertEqual("alpha", result.captured_outgoing)
        self.assertEqual(b"BETA-CREDS", backend.live_creds.read_bytes())
        self.assertEqual(b"LIVE-A", (self.cli_data("alpha") / CREDS).read_bytes())
        self.assert_no_tmp()

    def test_unpaired_target_signs_out_and_keeps_old_recoverable(self) -> None:
        backend = self.make()
        self.write_live(b"LIVE-A")

        result = backend.switch_to("beta", "alpha")

        self.assertTrue(result.ok and result.needs_login and not result.installed)
        self.assertFalse(backend.live_creds.exists())  # deliberately signed out
        self.assertEqual(b"LIVE-A", (self.cli_data("alpha") / CREDS).read_bytes())

    def test_live_absent_installs_target_without_capture(self) -> None:
        backend = self.make()
        self.write_store("beta", creds=b"BETA")

        result = backend.switch_to("beta", "alpha")

        self.assertTrue(result.installed and result.captured_outgoing == "")
        self.assertEqual(b"BETA", backend.live_creds.read_bytes())

    def test_unknown_outgoing_parks_in_unclaimed(self) -> None:
        backend = self.make()
        self.write_live(b"LIVE-A")
        self.write_store("beta", creds=b"BETA")

        result = backend.switch_to("beta", "")

        self.assertTrue(result.captured_outgoing.startswith("_unclaimed-"))
        self.assertEqual(b"LIVE-A", (self.cli_data(result.captured_outgoing) / CREDS).read_bytes())
        self.assertTrue(any("unknown" in warning for warning in result.warnings))

    def test_account_mismatch_parks_in_unclaimed(self) -> None:
        backend = self.make()
        self.write_live(b"LIVE-A")
        self.write_claude_json(uuid="LIVE-UUID")
        self.write_store("alpha", creds=b"ALPHA", account={"accountUuid": "OTHER-UUID"})
        self.write_store("beta", creds=b"BETA")

        result = backend.switch_to("beta", "alpha")

        self.assertTrue(result.captured_outgoing.startswith("_unclaimed-"))
        self.assertTrue(result.captured_outgoing.endswith("-alpha"))
        self.assertEqual(b"ALPHA", (self.cli_data("alpha") / CREDS).read_bytes())  # not overwritten
        self.assertEqual(b"LIVE-A", (self.cli_data(result.captured_outgoing) / CREDS).read_bytes())
        self.assertTrue(any("did not match" in warning for warning in result.warnings))

    def test_env_override_is_surfaced_in_warnings(self) -> None:
        backend = self.make(env_reader=lambda: {"ANTHROPIC_API_KEY": "sk-fixture"})
        self.write_store("beta", creds=b"BETA")

        overrides = backend.env_overrides()
        result = backend.switch_to("beta", "alpha")

        self.assertTrue(any("ANTHROPIC_API_KEY" in warning for warning in overrides))
        self.assertTrue(any("ANTHROPIC_API_KEY" in warning for warning in result.warnings))

    def test_atomic_replace_failure_leaves_live_absent_never_partial(self) -> None:
        backend = self.make()
        self.write_store("beta", creds=b"BETA")

        with patch("gui.cli_backend.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                backend.switch_to("beta", "alpha")

        self.assertFalse(backend.live_creds.exists())  # never a partially-written install

    # ----- pairing ----------------------------------------------------------

    def test_pair_polls_and_harvests_new_login(self) -> None:
        spawner = FakeSpawner(self.home / ".claude" / CREDS, create_on=1, payload=b"NEW")
        backend = self.make(spawner=spawner, which=lambda name: "C:/fake/claude.exe")

        result = backend.pair("beta")

        self.assertTrue(result.ok)
        self.assertEqual(["cmd.exe", "/k", str(Path("C:/fake/claude.exe"))], spawner.argv)
        self.assertEqual(b"NEW", (self.cli_data("beta") / CREDS).read_bytes())

    def test_pair_adopts_existing_live_without_spawning(self) -> None:
        self.write_live(b"LIVE")
        self.write_claude_json(uuid="U1", email="john@example.invalid")
        spawner = FakeSpawner(self.home / ".claude" / CREDS)
        backend = self.make(spawner=spawner)

        result = backend.pair("beta")

        self.assertTrue(result.ok)
        self.assertIsNone(spawner.argv)  # adopted live, never spawned a terminal
        self.assertEqual(b"LIVE", (self.cli_data("beta") / CREDS).read_bytes())
        self.assertEqual("john@example.invalid", result.account.email)

    def test_pair_cancelled_when_terminal_closes(self) -> None:
        spawner = FakeSpawner(self.home / ".claude" / CREDS, exit_on=1)
        backend = self.make(spawner=spawner, which=lambda name: "claude")

        result = backend.pair("beta")

        self.assertFalse(result.ok)
        self.assertTrue(result.cancelled)

    def test_pair_times_out(self) -> None:
        spawner = FakeSpawner(self.home / ".claude" / CREDS)  # never creates, never exits
        backend = self.make(spawner=spawner, which=lambda name: "claude",
                            pair_timeout=0.05, pair_interval=0.01)

        result = backend.pair("beta")

        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)

    def test_pair_soft_mismatch_warns_only(self) -> None:
        self.write_live(b"LIVE")
        self.write_claude_json(uuid="LIVE-UUID")
        backend = self.make()

        result = backend.pair("beta", expected_account_sha256=_sha256("DIFFERENT-UUID"))

        self.assertTrue(result.ok)  # warn-only: the login is still kept
        self.assertTrue(any("may not match" in warning for warning in result.warnings))

    # ----- resolve / info / lifecycle ---------------------------------------

    def test_resolve_cli_falls_back_to_local_bin(self) -> None:
        fallback = self.home / ".local" / "bin" / "claude.exe"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_bytes(b"")
        backend = self.make(which=lambda name: None)

        self.assertEqual(fallback, backend.resolve_cli())

    def test_resolve_cli_errors_when_missing(self) -> None:
        backend = self.make(which=lambda name: None)
        with self.assertRaises(CliBackendError) as caught:
            backend.resolve_cli()
        self.assertIn("claude.exe", str(caught.exception))

    def test_pair_info_reports_unpaired_then_paired(self) -> None:
        backend = self.make()
        self.assertFalse(backend.pair_info("beta").paired)

        self.write_store("beta", creds=b"BETA", account={"emailAddress": "b@example.invalid"})
        info = backend.pair_info("beta")
        self.assertTrue(info.paired)
        self.assertEqual("b@example.invalid", info.email)

    def test_rename_store_moves_credentials(self) -> None:
        backend = self.make()
        self.write_store("alpha", creds=b"ALPHA", account={"emailAddress": "a@example.invalid"})

        destination = backend.rename_store("alpha", "gamma")

        self.assertFalse(self.cli_data("alpha").exists())
        self.assertEqual(b"ALPHA", (destination / CREDS).read_bytes())
        self.assertTrue(backend.pair_info("gamma").paired)

    def test_retire_store_parks_with_name_suffix(self) -> None:
        backend = self.make()
        self.write_store("alpha", creds=b"ALPHA")

        destination = backend.retire_store("alpha")

        self.assertIsNotNone(destination)
        self.assertTrue(destination.name.startswith("_unclaimed-"))
        self.assertTrue(destination.name.endswith("-alpha"))
        self.assertFalse(self.cli_data("alpha").exists())
        self.assertEqual(b"ALPHA", (destination / CREDS).read_bytes())
        self.assertIsNone(backend.retire_store("never-existed"))

    # ----- unpair -----------------------------------------------------------

    def test_unpair_paired_parks_store_bytes_intact(self) -> None:
        backend = self.make()
        self.write_store("beta", creds=b"BETA-CREDS")

        result = backend.unpair("beta")

        self.assertTrue(result.ok and result.parked_store)
        self.assertEqual("", result.parked_live)
        self.assertFalse(self.cli_data("beta").exists())
        self.assertEqual(
            b"BETA-CREDS", (self.cli_data(result.parked_store) / CREDS).read_bytes())
        self.assert_no_tmp()

    def test_unpair_sign_out_live_also_parks_live_intact(self) -> None:
        backend = self.make()
        self.write_store("beta", creds=b"BETA-CREDS")
        self.write_live(b"LIVE-A")

        result = backend.unpair("beta", sign_out_live=True)

        self.assertTrue(result.ok and result.parked_store and result.parked_live)
        self.assertFalse(backend.live_creds.exists())  # CLI signed out, recoverable
        self.assertEqual(
            b"BETA-CREDS", (self.cli_data(result.parked_store) / CREDS).read_bytes())
        self.assertEqual(
            b"LIVE-A", (self.cli_data(result.parked_live) / CREDS).read_bytes())
        self.assert_no_tmp()

    def test_unpair_unpaired_without_live_is_noop(self) -> None:
        backend = self.make()

        result = backend.unpair("beta", sign_out_live=True)

        self.assertTrue(result.ok)
        self.assertEqual("", result.parked_store)
        self.assertEqual("", result.parked_live)
        self.assertIn("nothing needed parking", result.message)

    def test_unpair_live_absent_signs_out_nothing(self) -> None:
        backend = self.make()
        self.write_store("beta", creds=b"BETA-CREDS")  # paired, but no live file

        result = backend.unpair("beta", sign_out_live=True)

        self.assertTrue(result.ok and result.parked_store)
        self.assertEqual("", result.parked_live)  # no live file to sign out

    def test_unpair_then_pair_info_reports_unpaired(self) -> None:
        backend = self.make()
        self.write_store("beta", creds=b"BETA", account={"emailAddress": "b@example.invalid"})
        self.assertTrue(backend.pair_info("beta").paired)

        backend.unpair("beta")

        self.assertFalse(backend.pair_info("beta").paired)


if __name__ == "__main__":
    unittest.main()
