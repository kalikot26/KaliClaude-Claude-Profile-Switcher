from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gui.desktop_backend import DesktopBackend, _sha256


ACCOUNT_A = "11111111-1111-4111-8111-111111111111"
ACCOUNT_B = "22222222-2222-4222-8222-222222222222"
ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CARD = "local_33333333-3333-4333-8333-333333333333.json"
OTHER = "local_44444444-4444-4444-8444-444444444444.json"


class HistorySyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.platform = Mock()
        self.platform.desktop_processes.return_value = []
        self.backend = DesktopBackend(
            cache_dir=self.home / "cache", claude_dir=self.home / "default",
            process_adapter=self.platform,
        )
        self.alpha = self.profile("alpha", ACCOUNT_A, ORG_A)
        self.beta = self.profile("beta", ACCOUNT_B, ORG_B)

    def profile(self, name, account, organization):
        root = self.backend.desktop_data_dir / name
        folder = root / "claude-code-sessions" / account / organization
        folder.mkdir(parents=True)
        (root / "config.json").write_text(json.dumps({
            "lastKnownAccountUuid": account, "oauth:tokenCacheV2": "opaque-" + name,
        }))
        for relative in ("Network/Cookies", "Local Storage/store", "IndexedDB/db", ".credentials.json"):
            path = root / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes((name + relative).encode())
        # Actual runtime layout and suffix. Logs are evidence, not path authority.
        log = root / "logs" / "main.log"
        log.parent.mkdir()
        log.write_text(f"Loaded 16 persisted sessions from {folder} (51 archived deferred)\n")
        meta = self.backend._load_meta()
        meta["profiles"][name] = {
            "root_path": str(root), "storage_mode": "isolated",
            "account_id_sha256": _sha256(account),
        }
        self.backend._save_meta(meta)
        return folder

    @staticmethod
    def card(folder, name=CARD, activity="2026-09-11T05:00:00Z", **extra):
        value = {"lastActivityAt": activity, "isArchived": False, "title": "fixture", **extra}
        path = folder / name
        path.write_text(json.dumps(value))
        return path

    @staticmethod
    def snapshot(root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_real_account_org_paths_share_only_into_each_own_folder(self):
        a = self.card(self.alpha)
        b = self.card(self.beta, OTHER, isArchived=True)
        before = self.snapshot(self.backend.desktop_data_dir)
        report = self.backend.sync_histories()
        self.assertTrue(report.ok, report.message)
        self.assertTrue((self.beta / CARD).is_file(), "missing card was not shared into destination account/org")
        self.assertEqual((2, 0, 2, 0, 2), (report.added, report.updated, report.skipped, report.failed, report.folders))
        self.assertEqual(a.read_bytes(), (self.beta / CARD).read_bytes())
        self.assertEqual(b.read_bytes(), (self.alpha / OTHER).read_bytes())
        self.assertTrue(json.loads((self.alpha / OTHER).read_bytes())["isArchived"])
        after = self.snapshot(self.backend.desktop_data_dir)
        self.assertEqual(before, {name: after[name] for name in before})
        self.assertEqual(len(before) + 2, len(after))
        self.platform.request_close.assert_not_called()
        self.platform.force_stop.assert_not_called()
        self.platform.launch.assert_not_called()
        self.assertIsNotNone(report.backup)
        self.assertEqual(2, len(json.loads((report.backup.path / "index.json").read_bytes())))

    def test_last_activity_beats_mtime_and_backup_precedes_replacement(self):
        a = self.card(self.alpha, activity=1_789_100_000_000, title="recent")
        b = self.card(self.beta, activity=1_789_000_000_000, title="stale")
        old = b.read_bytes()
        os.utime(a, (100, 100))
        os.utime(b, (2_000_000_000, 2_000_000_000))
        original_replace = os.replace

        def replace(source, destination):
            if Path(destination) == b:
                saved_name = _sha256(str(b.relative_to(self.backend.desktop_data_dir))) + ".json"
                backups = list(self.backend.backups_dir.glob("cc-sync-*/" + saved_name))
                self.assertEqual([old], [p.read_bytes() for p in backups])
                self.assertEqual(old, b.read_bytes())
                self.assertEqual(a.read_bytes(), Path(source).read_bytes())
            return original_replace(source, destination)

        with patch("gui.desktop_backend.os.replace", side_effect=replace):
            report = self.backend.sync_histories()
        self.assertTrue(report.ok, report.message)
        self.assertEqual((0, 1, 0), (report.added, report.updated, report.failed))
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_iso_timezones_are_compared_as_instants(self):
        a = self.card(self.alpha, activity="2026-09-11T13:00:00+08:00", title="old")
        b = self.card(self.beta, activity="2026-09-11T06:00:00Z", title="new")
        report = self.backend.sync_histories()
        self.assertEqual(1, report.updated, report.message)
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_unknown_or_equal_freshness_preserves_conflicting_copies(self):
        for activity in (None, "bad", True, float("nan"), 10 ** 400, "2026-09-11T05:00:00Z"):
            with self.subTest(activity=activity):
                a = self.card(self.alpha, activity=activity, title="A")
                b = self.card(self.beta, title="B")
                before = (a.read_bytes(), b.read_bytes())
                report = self.backend.sync_histories()
                self.assertTrue(report.ok, report.message)
                self.assertEqual((0, 0, 2, 1), (report.added, report.updated, report.skipped, report.conflicts))
                self.assertEqual(before, (a.read_bytes(), b.read_bytes()))

    def test_identical_missing_activity_can_copy_without_guessing_a_winner(self):
        a = self.card(self.alpha, activity=None)
        report = self.backend.sync_histories()
        self.assertEqual(1, report.added, report.message)
        self.assertEqual(a.read_bytes(), (self.beta / CARD).read_bytes())

    def test_archive_disagreement_never_unarchives_or_broadcasts_to_absent_target(self):
        gamma = self.profile("gamma", ACCOUNT_A, ORG_A)
        self.card(self.alpha, isArchived=True, archivedAt="2026-09-10T00:00:00Z")
        self.card(self.beta, activity="2026-09-11T06:00:00Z", isArchived=False)
        before = self.snapshot(self.backend.desktop_data_dir)
        report = self.backend.sync_histories()
        self.assertEqual(1, report.conflicts)
        self.assertEqual(before, self.snapshot(self.backend.desktop_data_dir))
        self.assertFalse((gamma / CARD).exists())

    def test_absence_and_legacy_manifest_never_propagate_deletions(self):
        a = self.card(self.alpha)
        self.assertEqual(1, self.backend.sync_histories().added)
        # Simulates a disappeared source card after a previous sync.
        a.unlink()
        self.backend.history_manifest.write_text(json.dumps({"legacy-key": [CARD]}))
        manifest = self.backend.history_manifest.read_bytes()
        report = self.backend.sync_histories()
        self.assertEqual((1, 0), (report.added, report.removed))
        self.assertEqual(a.read_bytes(), (self.beta / CARD).read_bytes())
        self.assertEqual(manifest, self.backend.history_manifest.read_bytes())
        before = self.snapshot(self.home)
        rerun = self.backend.sync_histories()
        self.assertEqual((0, 0, 2, 0), (rerun.added, rerun.updated, rerun.skipped, rerun.failed))
        self.assertIsNone(rerun.backup)
        self.assertEqual(before, self.snapshot(self.home))

    def test_failed_atomic_replace_retains_old_bytes_and_retry_succeeds(self):
        self.card(self.alpha, activity="2026-09-11T06:00:00Z")
        b = self.card(self.beta)
        before = self.snapshot(self.backend.desktop_data_dir)
        original_replace = os.replace

        def replace(source, destination):
            if Path(destination) == b:
                raise OSError("fixture replace failure")
            return original_replace(source, destination)

        with patch("gui.desktop_backend.os.replace", side_effect=replace):
            report = self.backend.sync_histories()
        self.assertFalse(report.ok)
        self.assertEqual((0, 0, 1), (report.added, report.updated, report.failed))
        self.assertEqual(before, self.snapshot(self.backend.desktop_data_dir))
        self.assertIn(b.read_bytes(), self.snapshot(report.backup.path).values())
        self.assertEqual(1, self.backend.sync_histories().updated)

    def test_failed_staged_create_retains_source_and_counts_partial_success(self):
        self.card(self.alpha)
        self.card(self.alpha, OTHER)
        original_link = os.link

        def link(source, destination):
            if Path(destination).name == CARD:
                raise OSError("fixture create failure")
            return original_link(source, destination)

        with patch("gui.desktop_backend.os.link", side_effect=link):
            report = self.backend.sync_histories()
        self.assertFalse(report.ok)
        self.assertEqual((1, 1), (report.added, report.failed))
        self.assertFalse((self.beta / CARD).exists())
        self.assertTrue((self.alpha / CARD).is_file())
        self.assertTrue((self.beta / OTHER).is_file())
        self.assertFalse(list(self.backend.desktop_data_dir.rglob("*.tmp")))
        self.assertEqual(1, self.backend.sync_histories().added)

    def test_backup_failure_prevents_replacement(self):
        self.card(self.alpha, activity="2026-09-11T06:00:00Z")
        self.card(self.beta)
        before = self.snapshot(self.backend.desktop_data_dir)
        with patch.object(self.backend, "_backup_history_card", side_effect=OSError("backup full")):
            report = self.backend.sync_histories()
        self.assertFalse(report.ok)
        self.assertEqual(before, self.snapshot(self.backend.desktop_data_dir))

    def test_post_commit_cleanup_error_still_reports_the_committed_addition(self):
        self.card(self.alpha)
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.parent == self.beta and path.suffix == ".tmp":
                raise OSError("fixture cleanup failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            report = self.backend.sync_histories()
        self.assertTrue(report.ok)
        self.assertEqual((1, 0), (report.added, report.failed))
        self.assertIn("Card saved; temporary file remains", report.message)
        self.assertEqual((self.alpha / CARD).read_bytes(), (self.beta / CARD).read_bytes())
        self.assertEqual(0, self.backend.sync_histories().added)

    def test_concurrent_destination_creation_or_edit_is_not_overwritten(self):
        self.card(self.alpha, activity="2026-09-11T06:00:00Z")
        concurrent = b'{"title":"concurrent edit"}'
        self.backend._fault_hook = lambda _step: (self.beta / CARD).write_bytes(concurrent)
        report = self.backend.sync_histories()
        self.assertFalse(report.ok)
        self.assertEqual(concurrent, (self.beta / CARD).read_bytes())
        self.card(self.beta)
        report = self.backend.sync_histories()
        self.assertFalse(report.ok)
        self.assertEqual(concurrent, (self.beta / CARD).read_bytes())

    def test_running_profile_updates_are_skipped_without_stopping_sessions(self):
        self.card(self.alpha, activity="2026-09-11T06:00:00Z")
        self.card(self.beta)
        before = self.snapshot(self.backend.desktop_data_dir)
        with patch.object(self.backend, "_root_is_running", return_value=True):
            report = self.backend.sync_histories()
        self.assertTrue(report.ok, report.message)
        self.assertEqual(0, report.updated)
        self.assertIn("close it and sync again", report.message)
        self.assertEqual(before, self.snapshot(self.backend.desktop_data_dir))
        self.platform.request_close.assert_not_called()
        self.platform.force_stop.assert_not_called()

    def test_default_foreign_and_agent_mode_files_and_jsonl_are_not_read_or_written(self):
        self.card(self.alpha)
        for root in (self.backend.claude_dir, self.home / "foreign"):
            folder = root / "claude-code-sessions" / ACCOUNT_A / ORG_A
            folder.mkdir(parents=True)
            self.card(folder, OTHER)
            (root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": ACCOUNT_A}))
        meta = self.backend._load_meta()
        for name, root in (("default", self.backend.claude_dir), ("foreign", self.home / "foreign")):
            meta["profiles"][name] = {"root_path": str(root), "account_id_sha256": _sha256(ACCOUNT_A)}
        self.backend._save_meta(meta)
        foreign_account = self.alpha.parents[1] / ACCOUNT_B / ORG_B
        foreign_account.mkdir(parents=True)
        self.card(foreign_account, OTHER)
        agent = self.alpha.parents[2] / "local-agent-mode-sessions" / ACCOUNT_A / ORG_A
        agent.mkdir(parents=True)
        self.card(agent, OTHER)
        jsonl = self.alpha / "do-not-read.jsonl"
        jsonl.write_bytes(b"opaque transcript")
        transcript = self.home / ".claude" / "projects" / "project" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes(b"global transcript")
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            self.assertFalse(path.suffix == ".jsonl", f"JSONL I/O: {path}")
            self.assertNotIn("local-agent-mode-sessions", path.parts)
            self.assertNotIn(self.backend.claude_dir, path.parents)
            self.assertNotIn(self.home / "foreign", path.parents)
            return original_open(path, *args, **kwargs)

        before = self.snapshot(self.home)
        with patch.object(Path, "open", guarded_open):
            report = self.backend.sync_histories()
        self.assertTrue(report.ok, report.message)
        self.assertEqual((1, 2), (report.added, report.profiles_skipped))
        after = self.snapshot(self.home)
        self.assertEqual(before, {name: after[name] for name in before})
        self.assertFalse((self.beta / OTHER).exists())

    def test_missing_ambiguous_or_mismatched_identity_is_actionable_and_skipped(self):
        self.card(self.alpha)
        extra = self.beta.parent / ORG_A
        extra.mkdir()
        report = self.backend.sync_histories()
        self.assertEqual((0, 1), (report.added, report.profiles_skipped))
        self.assertIn("no single verified account/org", report.message)
        extra.rmdir()
        config = self.beta.parents[2] / "config.json"
        for value in ({}, {"lastKnownAccountUuid": "../escape"}, {"lastKnownAccountUuid": ACCOUNT_A}):
            config.write_text(json.dumps(value))
            report = self.backend.sync_histories()
            self.assertEqual((0, 1), (report.added, report.profiles_skipped))
            self.assertFalse((self.beta / CARD).exists())

    def test_malformed_card_blocks_older_source_broadcast(self):
        self.card(self.alpha)
        (self.beta / CARD).write_bytes(b"{partial")
        before = self.snapshot(self.backend.desktop_data_dir)
        report = self.backend.sync_histories()
        self.assertFalse(report.ok)
        self.assertEqual((0, 0, 1), (report.added, report.updated, report.failed))
        self.assertEqual(before, self.snapshot(self.backend.desktop_data_dir))

    def test_unsafe_path_segments_are_rejected(self):
        for segment in ("%2e%2e", "../escape", "..", "foo:bar", "x\\y", "x/y", "trailing."):
            with self.subTest(segment=segment), self.assertRaises(RuntimeError):
                self.backend._contained_path(self.alpha, segment)

    def test_symlink_card_cannot_escape_to_credentials(self):
        secret = self.home / "opaque-auth.json"
        secret.write_bytes(b'{"lastActivityAt": 1000, "secret": "fixture"}')
        try:
            (self.alpha / CARD).symlink_to(secret)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        before = secret.read_bytes()
        report = self.backend.sync_histories()
        self.assertFalse(report.ok)
        self.assertEqual(before, secret.read_bytes())
        self.assertFalse((self.beta / CARD).exists())


if __name__ == "__main__":
    unittest.main()
