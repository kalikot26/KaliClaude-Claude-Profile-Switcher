from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gui.cli_backend import CliBackend, CliBackendError
from gui.desktop_backend import _sha256

POOL = "pool"

CREDS = ".credentials.json"


class FakeSpawner:
    """Scriptable stand-in for a spawned login terminal.

    create_on/exit_on are 1-based poll counts: on the Nth poll() the spawner
    writes the live credentials file / reports the process as exited.
    """

    def __init__(self, live_creds: Path, *, create_on=None, exit_on=None,
                 payload=b"NEWCLI", local_oauth=None):
        self.live_creds = live_creds
        self.create_on = create_on
        self.exit_on = exit_on
        self.payload = payload
        # When set, a pool login (isolated CLAUDE_CONFIG_DIR) also writes that
        # dir's own .claude.json — as the current CLI does — carrying the true
        # oauthAccount while the shared ~\.claude.json stays stale.
        self.local_oauth = local_oauth
        self.polls = 0
        self.argv = None
        self.env = None

    def __call__(self, argv, env=None):
        self.argv = argv
        self.env = env
        return self

    def _target(self) -> Path:
        # A pool login writes into the child's isolated CLAUDE_CONFIG_DIR; the
        # partner-model pair login writes the shared live store.
        if self.env and self.env.get("CLAUDE_CONFIG_DIR"):
            return Path(self.env["CLAUDE_CONFIG_DIR"]) / CREDS
        return self.live_creds

    def poll(self):
        self.polls += 1
        if self.create_on is not None and self.polls >= self.create_on:
            target = self._target()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.payload)
            if self.local_oauth is not None and self.env and self.env.get("CLAUDE_CONFIG_DIR"):
                (Path(self.env["CLAUDE_CONFIG_DIR"]) / ".claude.json").write_text(
                    json.dumps({"oauthAccount": self.local_oauth}))
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
        for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                     "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
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

    # ----- pool -------------------------------------------------------------

    def pool(self, *parts) -> Path:
        return self.cli_data(POOL).joinpath(*parts)

    def pool_order(self) -> list:
        return json.loads(self.pool("pool.json").read_text())["order"]

    def test_pool_add_harvests_identity_and_appends_order(self) -> None:
        # No config-dir-local .claude.json here (spawner writes only creds), so
        # harvest falls back to the shared ~\.claude.json — the (b) fallback path.
        self.write_claude_json(uuid="POOL-UUID", email="pool@example.invalid")
        spawner = FakeSpawner(self.home / ".claude" / CREDS, create_on=1, payload=b"POOL-CREDS")
        backend = self.make(spawner=spawner, which=lambda name: "C:/fake/claude.exe")

        result = backend.pool_add("work")

        self.assertTrue(result.ok and not result.cancelled and not result.timed_out)
        self.assertEqual("pool@example.invalid", result.email)
        self.assertEqual(["cmd.exe", "/k", str(Path("C:/fake/claude.exe"))], spawner.argv)
        self.assertEqual(str(self.pool("work")), spawner.env["CLAUDE_CONFIG_DIR"])
        self.assertEqual(b"POOL-CREDS", (self.pool("work") / CREDS).read_bytes())
        self.assertEqual(["work"], self.pool_order())
        account = backend.pool_list()[0]
        self.assertTrue(account.logged_in)
        self.assertEqual("pool@example.invalid", account.email)
        self.assertEqual("POOL-UUID", account.account_uuid)
        self.assert_no_tmp()

    def test_pool_add_prefers_config_dir_local_identity_over_stale_shared(self) -> None:
        # (a) The shared ~\.claude.json is stale (a previously-logged-in account);
        # the pool login writes its TRUE identity into the config dir's own
        # .claude.json. Harvest must take the local identity, not the stale shared.
        self.write_claude_json(uuid="STALE-UUID", email="stale@example.invalid")
        spawner = FakeSpawner(
            self.home / ".claude" / CREDS, create_on=1, payload=b"POOL-CREDS",
            local_oauth={"accountUuid": "FRESH-UUID", "emailAddress": "fresh@example.invalid"})
        backend = self.make(spawner=spawner, which=lambda name: "C:/fake/claude.exe")

        result = backend.pool_add("work")

        self.assertTrue(result.ok)
        self.assertEqual("fresh@example.invalid", result.email)
        account = backend.pool_list()[0]
        self.assertEqual("fresh@example.invalid", account.email)
        self.assertEqual("FRESH-UUID", account.account_uuid)
        # Read-only: the shared file was never rewritten and stays stale.
        self.assertEqual(
            "stale@example.invalid",
            json.loads((self.home / ".claude.json").read_text())["oauthAccount"]["emailAddress"])
        # Read-only: the pool dir's own .claude.json is untouched too.
        self.assertEqual(
            "fresh@example.invalid",
            json.loads((self.pool("work") / ".claude.json").read_text())["oauthAccount"]["emailAddress"])
        self.assert_no_tmp()

    def test_pool_add_refuses_duplicate(self) -> None:
        backend = self.make(which=lambda name: "claude")
        backend._write_pool_order(["work"])
        with self.assertRaises(CliBackendError):
            backend.pool_add("work")

    def test_pool_add_refuses_invalid_names(self) -> None:
        backend = self.make(which=lambda name: "claude")
        for bad in ("bad name", "has/slash", "", "a" * 65, "no.dots"):
            with self.subTest(bad=bad), self.assertRaises(CliBackendError):
                backend.pool_add(bad)

    def test_pool_add_refuses_underscore_prefix(self) -> None:
        backend = self.make(which=lambda name: "claude")
        with self.assertRaises(CliBackendError):
            backend.pool_add("_retired-x")

    def test_pool_add_cancelled_when_terminal_closes(self) -> None:
        spawner = FakeSpawner(self.home / ".claude" / CREDS, exit_on=1)
        backend = self.make(spawner=spawner, which=lambda name: "claude")

        result = backend.pool_add("work")

        self.assertFalse(result.ok)
        self.assertTrue(result.cancelled)
        self.assertFalse(self.pool("pool.json").exists())  # never added to order

    def test_pool_add_times_out(self) -> None:
        spawner = FakeSpawner(self.home / ".claude" / CREDS)  # never creates, never exits
        backend = self.make(spawner=spawner, which=lambda name: "claude",
                            pair_timeout=0.05, pair_interval=0.01)

        result = backend.pool_add("work")

        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)

    def test_pool_retire_parks_bytes_intact_and_drops_order(self) -> None:
        backend = self.make()
        self.pool("work").mkdir(parents=True)
        (self.pool("work") / CREDS).write_bytes(b"WORK-CREDS")
        backend._write_pool_order(["work", "home"])

        destination = backend.pool_retire("work")

        self.assertIsNotNone(destination)
        self.assertTrue(destination.name.startswith("_retired-"))
        self.assertTrue(destination.name.endswith("-work"))
        self.assertFalse(self.pool("work").exists())
        self.assertEqual(b"WORK-CREDS", (destination / CREDS).read_bytes())
        self.assertEqual(["home"], backend._pool_order())
        self.assertIsNone(backend.pool_retire("never-existed"))

    def test_pool_move_reorders_and_clamps(self) -> None:
        backend = self.make()
        backend._write_pool_order(["a", "b", "c"])

        self.assertEqual(["b", "a", "c"], backend.pool_move("a", 1))
        self.assertEqual(["b", "a", "c"], backend.pool_move("b", -1))   # clamp at top
        self.assertEqual(["b", "a", "c"], backend.pool_move("c", 1))    # clamp at bottom
        self.assertEqual(["b", "c", "a"], backend.pool_move("c", -1))
        self.assertEqual(["b", "c", "a"], backend.pool_move("missing", 1))  # no-op
        self.assertEqual(["b", "c", "a"], self.pool_order())

    def test_pool_list_merges_strays_and_excludes_retired(self) -> None:
        backend = self.make()
        self.pool("work").mkdir(parents=True)
        (self.pool("work") / CREDS).write_bytes(b"W")
        (self.pool("work") / "account.json").write_text(
            json.dumps({"emailAddress": "w@example.invalid"}))
        self.pool("stray").mkdir(parents=True)               # on-disk, no creds
        self.pool("_retired-20200101-000000-old").mkdir(parents=True)
        backend._write_pool_order(["work"])

        accounts = backend.pool_list()
        names = [account.name for account in accounts]

        self.assertEqual(["work", "stray"], names)           # order first, stray appended
        self.assertTrue(accounts[0].logged_in)
        self.assertEqual("w@example.invalid", accounts[0].email)
        self.assertFalse(accounts[1].logged_in)              # stray is signed out
        self.assertNotIn("_retired-20200101-000000-old", names)

    def test_pool_install_launcher_writes_both_files_with_failover_markers(self) -> None:
        backend = self.make()

        ps1 = backend.pool_install_launcher()

        self.assertEqual("claude-pool.ps1", ps1.name)
        self.assertTrue(ps1.is_file())
        cmd = ps1.with_name("claude-pool.cmd")
        self.assertTrue(cmd.is_file())
        ps1_text = ps1.read_text(encoding="utf-8")
        self.assertIn("failing over", ps1_text)
        self.assertIn("served by", ps1_text)
        self.assertIn("all accounts exhausted", ps1_text)
        self.assertIn(r"\b429\b", ps1_text)                  # limit-regex fragment
        self.assertIn("CLAUDE_CONFIG_DIR", ps1_text)
        self.assertIn(str(self.pool("pool.json")), ps1_text)  # concrete pool path injected
        self.assertIn("claude-pool.ps1", cmd.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
