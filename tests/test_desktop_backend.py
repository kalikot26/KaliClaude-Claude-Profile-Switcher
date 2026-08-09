from __future__ import annotations

import json
import hashlib
import inspect
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gui.desktop_backend as desktop_backend
import gui.app as app
from gui.desktop_backend import (
    DesktopBackend,
    ProcessDetectionError,
    ProfileState,
    RollbackError,
    SnapshotValidationError,
    UnknownLiveAccountError,
    WindowsDesktopProcessAdapter,
)


def fake_oauth_decoder(blob: str) -> dict | None:
    account = {
        "A_V1": "account-a",
        "A_V2": "account-a",
        "B_V2": "account-b",
        "C_V2": "account-c",
    }.get(blob)
    if blob == "EMPTY":
        return {}
    if not account:
        return None
    return {
        f"org:{account}:api:scope": {
            "token": f"token-{blob.lower()}",
            "expiresAt": 9_999_999_999_999,
        }
    }


def fake_cookie_decoder(value: bytes) -> str | None:
    return "session" if value.startswith(b"VALID-") else None


class FakeProcessAdapter:
    def __init__(self, *, running: bool = True, detection_error: bool = False) -> None:
        self.pids = [101, 202] if running else []
        self.detection_error = detection_error
        self.graceful: list[int] = []
        self.forced: list[int] = []

    def desktop_pids(self) -> list[int]:
        if self.detection_error:
            raise OSError("CIM unavailable")
        return list(self.pids)

    def request_close(self, pids: list[int]) -> None:
        self.graceful.extend(pids)
        self.pids.clear()

    def wait_stopped(self, _timeout: float) -> bool:
        return not self.pids

    def force_stop(self, pids: list[int]) -> None:
        self.forced.extend(pids)
        self.pids = [pid for pid in self.pids if pid not in pids]

    def launch(self) -> None:
        self.pids = [303]


class StubbornProcessAdapter(FakeProcessAdapter):
    def request_close(self, pids: list[int]) -> None:
        self.graceful.extend(pids)


class BackendTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.claude_dir = root / "Claude"
        self.cache_dir = root / "cache"
        self.backend = DesktopBackend(
            claude_dir=self.claude_dir,
            cache_dir=self.cache_dir,
            oauth_decoder=fake_oauth_decoder,
            cookie_decoder=fake_cookie_decoder,
            claude_version=lambda: "1.2.3",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_leveldb(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "CURRENT").write_text("MANIFEST-000001\n", encoding="ascii")
        (root / "MANIFEST-000001").write_bytes(b"manifest")
        (root / "000003.log").write_bytes(b"records")

    def create_session(self, root: Path, *, cookie: bytes = b"VALID-A") -> None:
        self.create_leveldb(root / "Session Storage")
        self.create_leveldb(root / "Local Storage" / "leveldb")
        self.create_leveldb(
            root / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb"
        )
        cookies = root / "Network" / "Cookies"
        cookies.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(cookies)) as db:
            db.execute(
                "CREATE TABLE cookies "
                "(host_key TEXT, name TEXT, encrypted_value BLOB)"
            )
            db.execute(
                "INSERT INTO cookies VALUES (?, ?, ?)",
                (".claude.ai", "sessionKey", cookie),
            )
            db.commit()

    def create_legacy_profile(self, name: str, oauth: str) -> Path:
        profile = self.cache_dir / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "oauth.blob").write_text(oauth, encoding="utf-8")
        self.create_session(profile / "session")
        return profile

    def write_meta(self, profiles: dict, active: str | None = None) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "meta.json").write_text(
            json.dumps({"active": active, "aumid": None, "profiles": profiles}),
            encoding="utf-8",
        )

    def create_live(
        self,
        *,
        v2: str,
        v1: str | None = None,
        cookie: bytes = b"VALID-LIVE",
    ) -> None:
        self.create_session(self.claude_dir, cookie=cookie)
        config = {"unrelated-setting": "preserved", "oauth:tokenCacheV2": v2}
        if v1 is not None:
            config["oauth:tokenCache"] = v1
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        (self.claude_dir / "config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )

    def switched_backend(
        self,
        *,
        process: FakeProcessAdapter | None = None,
        fault_hook=None,
        history_sync=None,
    ) -> DesktopBackend:
        return DesktopBackend(
            claude_dir=self.claude_dir,
            cache_dir=self.cache_dir,
            oauth_decoder=fake_oauth_decoder,
            cookie_decoder=fake_cookie_decoder,
            claude_version=lambda: "1.2.3",
            process_adapter=process or FakeProcessAdapter(),
            fault_hook=fault_hook,
            history_sync=history_sync,
        )

    @staticmethod
    def tree_digest(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def migrate_two_profiles(self, *, active: str = "a") -> None:
        self.create_legacy_profile("a", "A_V2")
        self.create_legacy_profile("b", "B_V2")
        self.write_meta(
            {"a": {"label": "Account A"}, "b": {"label": "Account B"}},
            active=active,
        )
        self.backend.audit_and_migrate()

    def test_audit_quarantines_empty_oauth_and_migrates_healthy_profile(self) -> None:
        healthy = self.create_legacy_profile("healthy", "A_V2")
        invalid = self.create_legacy_profile("invalid", "EMPTY")
        self.write_meta(
            {
                "healthy": {"label": "Healthy"},
                "invalid": {"label": "Invalid"},
            }
        )

        report = self.backend.audit_and_migrate()
        profiles = {profile.name: profile for profile in self.backend.list_profiles()}

        self.assertEqual(["healthy"], report.migrated)
        self.assertEqual(["invalid"], report.needs_relogin)
        self.assertTrue((healthy / "manifest.json").is_file())
        self.assertEqual("A_V2", (healthy / "oauth-v2.blob").read_text())
        self.assertFalse((invalid / "manifest.json").exists())
        self.assertTrue((invalid / "oauth.blob").exists(), "quarantine preserves files")
        self.assertEqual(ProfileState.READY, profiles["healthy"].state)
        self.assertEqual(ProfileState.NEEDS_RELOGIN, profiles["invalid"].state)
        self.assertIsNotNone(report.backup)
        self.assertTrue(report.backup.path.is_dir())

    def test_audit_is_idempotent(self) -> None:
        self.create_legacy_profile("healthy", "A_V2")
        self.write_meta({"healthy": {"label": "Healthy"}})

        first = self.backend.audit_and_migrate()
        manifest_before = (
            self.cache_dir / "profiles" / "healthy" / "manifest.json"
        ).read_bytes()
        second = self.backend.audit_and_migrate()

        self.assertEqual(["healthy"], first.migrated)
        self.assertEqual([], second.migrated)
        self.assertEqual([], second.needs_relogin)
        self.assertIsNone(second.backup)
        self.assertEqual(
            manifest_before,
            (self.cache_dir / "profiles" / "healthy" / "manifest.json").read_bytes(),
        )

    def test_migration_accepts_legacy_flat_oauth_blob(self) -> None:
        profile = self.cache_dir / "profiles" / "healthy"
        self.create_session(profile / "session")
        profile.parent.mkdir(parents=True, exist_ok=True)
        (profile.parent / "healthy.blob").write_text("A_V2", encoding="utf-8")
        self.write_meta({"healthy": {"label": "Healthy"}})

        report = self.backend.audit_and_migrate()

        self.assertEqual(["healthy"], report.migrated)
        self.assertEqual("A_V2", (profile / "oauth-v2.blob").read_text())
        self.assertTrue((profile.parent / "healthy.blob").is_file())

    def test_validation_rejects_missing_cookie_database(self) -> None:
        profile = self.create_legacy_profile("broken", "A_V2")
        (profile / "session" / "Network" / "Cookies").unlink()
        self.write_meta({"broken": {"label": "Broken"}})

        report = self.backend.audit_and_migrate()

        self.assertEqual(["broken"], report.needs_relogin)
        self.assertIn("Cookies", report.issues["broken"])

    def test_validation_rejects_corrupt_or_undecryptable_cookies(self) -> None:
        for cookie, expected in ((b"not sqlite", "corrupt"), (b"INVALID", "decryptable")):
            with self.subTest(expected=expected):
                name = "broken-" + expected
                profile = self.create_legacy_profile(name, "A_V2")
                cookies = profile / "session" / "Network" / "Cookies"
                if expected == "corrupt":
                    cookies.write_bytes(cookie)
                else:
                    with closing(sqlite3.connect(cookies)) as db:
                        db.execute("UPDATE cookies SET encrypted_value = ?", (cookie,))
                        db.commit()
                self.write_meta({name: {"label": name}})

                report = self.backend.audit_and_migrate()

                self.assertIn(name, report.needs_relogin)
                self.assertIn(expected, report.issues[name].lower())

    def test_validation_rejects_incomplete_leveldb_store(self) -> None:
        profile = self.create_legacy_profile("broken", "A_V2")
        (profile / "session" / "Local Storage" / "leveldb" / "CURRENT").unlink()
        self.write_meta({"broken": {"label": "Broken"}})

        report = self.backend.audit_and_migrate()

        self.assertEqual(["broken"], report.needs_relogin)
        self.assertIn("Local Storage", report.issues["broken"])

    def test_schema_two_keeps_v1_and_v2_oauth_caches_distinct(self) -> None:
        profile = self.create_legacy_profile("healthy", "A_V2")
        (profile / "oauth-v1.blob").write_text("A_V1", encoding="utf-8")
        (profile / "oauth-v2.blob").write_text("A_V2", encoding="utf-8")
        self.write_meta({"healthy": {"label": "Healthy"}})

        self.backend.audit_and_migrate()
        manifest = json.loads((profile / "manifest.json").read_text())

        self.assertEqual("A_V1", (profile / "oauth-v1.blob").read_text())
        self.assertEqual("A_V2", (profile / "oauth-v2.blob").read_text())
        self.assertEqual(2, manifest["schema"])
        self.assertEqual("valid", manifest["validation"]["status"])
        self.assertEqual("1.2.3", manifest["claude_desktop_version"])
        self.assertNotIn("account-a", json.dumps(manifest))

    def test_validation_rejects_v1_v2_account_identity_mismatch(self) -> None:
        profile = self.create_legacy_profile("mixed", "B_V2")
        (profile / "oauth-v1.blob").write_text("A_V1", encoding="utf-8")
        (profile / "oauth-v2.blob").write_text("B_V2", encoding="utf-8")
        self.write_meta({"mixed": {"label": "Mixed"}})

        report = self.backend.audit_and_migrate()

        self.assertEqual(["mixed"], report.needs_relogin)
        self.assertIn("different account", report.issues["mixed"])

    def test_manifest_oauth_metadata_must_match_snapshot_files(self) -> None:
        profile = self.create_legacy_profile("healthy", "A_V2")
        self.write_meta({"healthy": {"label": "Healthy"}})
        self.backend.audit_and_migrate()
        manifest_path = profile / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["oauth"]["v2"]["usable"] = False
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = self.backend.audit_and_migrate()

        self.assertEqual(["healthy"], report.corrupt)
        self.assertIn("OAuth metadata", report.issues["healthy"])

    def test_stale_active_metadata_cannot_overwrite_the_target_profile(self) -> None:
        self.migrate_two_profiles(active="a")
        original_a = self.tree_digest(self.cache_dir / "profiles" / "a")
        self.create_live(v2="B_V2", cookie=b"VALID-B-LIVE")

        result = self.switched_backend().switch("a")

        self.assertTrue(result.ok)
        self.assertEqual(original_a, self.tree_digest(self.cache_dir / "profiles" / "a"))
        with closing(
            sqlite3.connect(self.cache_dir / "profiles" / "b" / "session" / "Network" / "Cookies")
        ) as db:
            value = db.execute("SELECT encrypted_value FROM cookies").fetchone()[0]
        self.assertEqual(b"VALID-B-LIVE", bytes(value))
        live = json.loads((self.claude_dir / "config.json").read_text())
        self.assertEqual("A_V2", live["oauth:tokenCacheV2"])
        self.assertEqual("a", json.loads(self.backend.meta_file.read_text())["active"])

    def test_known_live_identity_reconciles_stale_metadata_without_restoring(self) -> None:
        self.migrate_two_profiles(active="a")
        self.create_live(v2="B_V2", cookie=b"VALID-B-LIVE")
        before = self.tree_digest(self.claude_dir)

        result = self.switched_backend().switch("b")

        self.assertTrue(result.ok)
        self.assertEqual(before, self.tree_digest(self.claude_dir))
        self.assertEqual("b", json.loads(self.backend.meta_file.read_text())["active"])

    def test_audit_reconciles_stale_metadata_from_known_live_identity(self) -> None:
        self.migrate_two_profiles(active="a")
        self.create_live(v2="B_V2")

        report = self.backend.audit_and_migrate()

        self.assertEqual("b", json.loads(self.backend.meta_file.read_text())["active"])
        self.assertIsNotNone(report.backup)

    def test_unknown_live_identity_aborts_without_changing_metadata_or_live_state(self) -> None:
        self.migrate_two_profiles(active="a")
        self.create_live(v2="C_V2")
        meta_before = self.backend.meta_file.read_bytes()
        live_before = self.tree_digest(self.claude_dir)

        with self.assertRaises(UnknownLiveAccountError):
            self.switched_backend().switch("b")

        self.assertEqual(meta_before, self.backend.meta_file.read_bytes())
        self.assertEqual(live_before, self.tree_digest(self.claude_dir))

    def test_stale_metadata_never_marks_a_profile_active_without_live_match(self) -> None:
        self.migrate_two_profiles(active="a")
        self.create_live(v2="C_V2")

        states = {profile.name: profile.state for profile in self.backend.list_profiles()}

        self.assertEqual(ProfileState.READY, states["a"])
        self.assertEqual(ProfileState.READY, states["b"])

    def test_process_detection_error_fails_closed(self) -> None:
        self.migrate_two_profiles(active="a")
        self.create_live(v2="A_V2")
        process = FakeProcessAdapter(detection_error=True)
        before = self.tree_digest(self.claude_dir)

        with self.assertRaises(ProcessDetectionError):
            self.switched_backend(process=process).switch("b")

        self.assertEqual(before, self.tree_digest(self.claude_dir))

    def test_windows_process_detection_excludes_claude_code(self) -> None:
        rows = [
            {
                "ProcessId": 10,
                "ExecutablePath": r"C:\Users\Me\AppData\Local\AnthropicClaude\app-1.2.3\claude.exe",
            },
            {
                "ProcessId": 20,
                "ExecutablePath": r"C:\Users\Me\AppData\Roaming\Claude\claude-code\1.0\claude.exe",
            },
        ]
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

        with patch.object(desktop_backend.subprocess, "run", return_value=completed):
            pids = WindowsDesktopProcessAdapter().desktop_pids()

        self.assertEqual([10], pids)

    def test_windows_process_detection_rejects_unidentified_claude_process(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ProcessId": 10, "ExecutablePath": None}),
            stderr="",
        )

        with patch.object(desktop_backend.subprocess, "run", return_value=completed):
            with self.assertRaises(OSError):
                WindowsDesktopProcessAdapter().desktop_pids()

    def test_windows_process_detection_rejects_unknown_claude_executable(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ProcessId": 10,
                    "ExecutablePath": r"C:\Portable\Claude\claude.exe",
                }
            ),
            stderr="",
        )

        with patch.object(desktop_backend.subprocess, "run", return_value=completed):
            with self.assertRaises(OSError):
                WindowsDesktopProcessAdapter().desktop_pids()

    def test_windows_process_detection_accepts_registered_store_desktop(self) -> None:
        executable = (
            r"C:\Program Files\WindowsApps\Claude_1.26832.0.0_x64__pzs8sxrjxfjjc"
            r"\app\Claude.exe"
        )
        process_query = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ProcessId": 10, "ExecutablePath": executable}),
            stderr="",
        )
        package_query = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(executable),
            stderr="",
        )

        with patch.object(
            desktop_backend.subprocess,
            "run",
            side_effect=[process_query, package_query],
        ):
            pids = WindowsDesktopProcessAdapter().desktop_pids()

        self.assertEqual([10], pids)

    def test_windows_process_detection_rejects_unregistered_store_executable(self) -> None:
        registered = (
            r"C:\Program Files\WindowsApps\Claude_1.26832.0.0_x64__pzs8sxrjxfjjc"
            r"\app\Claude.exe"
        )
        unregistered = (
            r"C:\Program Files\WindowsApps\Claude_9.9.9.9_x64__pzs8sxrjxfjjc"
            r"\app\Claude.exe"
        )
        process_query = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ProcessId": 10, "ExecutablePath": unregistered}),
            stderr="",
        )
        package_query = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(registered),
            stderr="",
        )

        with patch.object(
            desktop_backend.subprocess,
            "run",
            side_effect=[process_query, package_query],
        ):
            with self.assertRaises(OSError):
                WindowsDesktopProcessAdapter().desktop_pids()

    def test_force_stop_targets_only_the_verified_desktop_pids(self) -> None:
        process = StubbornProcessAdapter()
        backend = self.switched_backend(process=process)

        backend.stop_desktop()

        self.assertEqual([101, 202], process.graceful)
        self.assertEqual([101, 202], process.forced)

    def test_restore_preserves_distinct_v1_and_v2_caches(self) -> None:
        profile_a = self.create_legacy_profile("a", "A_V2")
        (profile_a / "oauth-v1.blob").write_text("A_V1", encoding="utf-8")
        (profile_a / "oauth-v2.blob").write_text("A_V2", encoding="utf-8")
        self.create_legacy_profile("b", "B_V2")
        self.write_meta({"a": {"label": "A"}, "b": {"label": "B"}}, active="b")
        self.backend.audit_and_migrate()
        self.create_live(v2="B_V2")

        self.switched_backend().switch("a")

        config = json.loads((self.claude_dir / "config.json").read_text())
        self.assertEqual("A_V1", config["oauth:tokenCache"])
        self.assertEqual("A_V2", config["oauth:tokenCacheV2"])

    def test_switch_replaces_cookie_wal_and_shm_with_target_versions(self) -> None:
        profile_a = self.create_legacy_profile("a", "A_V2")
        profile_b = self.create_legacy_profile("b", "B_V2")
        (profile_a / "session" / "Network" / "Cookies-wal").write_bytes(b"A-WAL")
        (profile_a / "session" / "Network" / "Cookies-shm").write_bytes(b"A-SHM")
        (profile_b / "session" / "Network" / "Cookies-wal").write_bytes(b"B-WAL")
        (profile_b / "session" / "Network" / "Cookies-shm").write_bytes(b"B-SHM")
        self.write_meta({"a": {"label": "A"}, "b": {"label": "B"}}, active="b")
        self.backend.audit_and_migrate()
        self.create_live(v2="B_V2")
        (self.claude_dir / "Network" / "Cookies-wal").write_bytes(b"LIVE-B-WAL")
        (self.claude_dir / "Network" / "Cookies-shm").write_bytes(b"LIVE-B-SHM")

        self.switched_backend().switch("a")

        self.assertEqual(
            b"A-WAL", (self.claude_dir / "Network" / "Cookies-wal").read_bytes()
        )
        self.assertEqual(
            b"A-SHM", (self.claude_dir / "Network" / "Cookies-shm").read_bytes()
        )

    def test_cookie_validation_does_not_modify_saved_sidecars(self) -> None:
        profile = self.create_legacy_profile("a", "A_V2")
        wal = profile / "session" / "Network" / "Cookies-wal"
        shm = profile / "session" / "Network" / "Cookies-shm"
        wal.write_bytes(b"A-WAL")
        shm.write_bytes(b"A-SHM")
        before = {"wal": wal.read_bytes(), "shm": shm.read_bytes()}
        self.write_meta({"a": {"label": "A"}})

        self.backend.audit_and_migrate()

        self.assertEqual(before["wal"], wal.read_bytes())
        self.assertEqual(before["shm"], shm.read_bytes())

    def test_switch_removes_cookie_sidecars_absent_from_target(self) -> None:
        self.migrate_two_profiles(active="b")
        self.create_live(v2="B_V2")
        (self.claude_dir / "Network" / "Cookies-wal").write_bytes(b"LIVE-B-WAL")
        (self.claude_dir / "Network" / "Cookies-shm").write_bytes(b"LIVE-B-SHM")

        self.switched_backend().switch("a")

        self.assertFalse((self.claude_dir / "Network" / "Cookies-wal").exists())
        self.assertFalse((self.claude_dir / "Network" / "Cookies-shm").exists())

    def test_capture_failure_preserves_previous_valid_snapshot(self) -> None:
        self.migrate_two_profiles(active="b")
        self.create_live(v2="B_V2", cookie=b"VALID-B-LIVE")
        before = self.tree_digest(self.cache_dir / "profiles" / "b")

        def fail(step: str) -> None:
            if step == "capture:Local Storage":
                raise OSError("injected capture failure")

        with self.assertRaises(OSError):
            self.switched_backend(fault_hook=fail).capture_current("b", "B", "")

        self.assertEqual(before, self.tree_digest(self.cache_dir / "profiles" / "b"))

    def test_update_ready_profile_rejects_a_different_live_account(self) -> None:
        self.migrate_two_profiles(active="a")
        self.create_live(v2="B_V2")
        profile_before = self.tree_digest(self.cache_dir / "profiles" / "a")

        with self.assertRaises(SnapshotValidationError):
            self.switched_backend().capture_current("a", "A", "")

        self.assertEqual(profile_before, self.tree_digest(self.cache_dir / "profiles" / "a"))

    def test_save_rejects_duplicate_profile_for_the_same_account(self) -> None:
        self.migrate_two_profiles(active="a")
        self.create_live(v2="A_V2")

        with self.assertRaises(SnapshotValidationError):
            self.switched_backend().capture_current("duplicate", "Duplicate", "")

        self.assertFalse((self.cache_dir / "profiles" / "duplicate").exists())

    def test_prepare_new_login_refreshes_known_profile_then_clears_live_state(self) -> None:
        self.migrate_two_profiles(active="a")
        self.create_live(v2="A_V2", cookie=b"VALID-A-FRESH")

        backup = self.switched_backend().prepare_new_login()

        self.assertTrue(backup.path.is_dir())
        self.assertTrue((backup.path / "backup-manifest.json").is_file())
        config = json.loads((self.claude_dir / "config.json").read_text())
        self.assertNotIn("oauth:tokenCache", config)
        self.assertNotIn("oauth:tokenCacheV2", config)
        self.assertFalse((self.claude_dir / "Network" / "Cookies").exists())
        with closing(
            sqlite3.connect(self.cache_dir / "profiles" / "a" / "session" / "Network" / "Cookies")
        ) as db:
            value = db.execute("SELECT encrypted_value FROM cookies").fetchone()[0]
        self.assertEqual(b"VALID-A-FRESH", bytes(value))
        self.assertIsNone(json.loads(self.backend.meta_file.read_text())["active"])

    def test_backup_failure_prevents_restoration_and_metadata_change(self) -> None:
        self.migrate_two_profiles(active="b")
        self.create_live(v2="B_V2")
        live_before = self.tree_digest(self.claude_dir)
        meta_before = self.backend.meta_file.read_bytes()

        def fail(step: str) -> None:
            if step == "backup:config":
                raise OSError("injected backup failure")

        with self.assertRaises(OSError):
            self.switched_backend(fault_hook=fail).switch("a")

        self.assertEqual(live_before, self.tree_digest(self.claude_dir))
        self.assertEqual(meta_before, self.backend.meta_file.read_bytes())

    def test_each_restore_failure_rolls_back_complete_live_state(self) -> None:
        steps = [
            "restore:Session Storage",
            "restore:IndexedDB",
            "restore:Local Storage",
            "restore:Network/Cookies",
            "restore:Network/Cookies-journal",
            "restore:Network/Cookies-wal",
            "restore:Network/Cookies-shm",
            "restore:config",
            "metadata",
        ]
        for step in steps:
            with self.subTest(step=step), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                claude = root / "Claude"
                cache = root / "cache"
                backend = DesktopBackend(
                    claude_dir=claude,
                    cache_dir=cache,
                    oauth_decoder=fake_oauth_decoder,
                    cookie_decoder=fake_cookie_decoder,
                    claude_version=lambda: "1.2.3",
                )
                old_claude, old_cache, old_backend = self.claude_dir, self.cache_dir, self.backend
                self.claude_dir, self.cache_dir, self.backend = claude, cache, backend
                try:
                    self.migrate_two_profiles(active="b")
                    self.create_live(v2="B_V2", cookie=b"VALID-B-LIVE")
                    (claude / "Network" / "Cookies-wal").write_bytes(b"LIVE-B-WAL")
                    (claude / "Network" / "Cookies-shm").write_bytes(b"LIVE-B-SHM")
                    live_before = self.tree_digest(claude)
                    meta_before = backend.meta_file.read_bytes()

                    def fail(current: str, expected: str = step) -> None:
                        if current == expected:
                            raise OSError("injected restore failure")

                    with self.assertRaises(OSError):
                        self.switched_backend(fault_hook=fail).switch("a")

                    self.assertEqual(live_before, self.tree_digest(claude))
                    self.assertEqual(meta_before, backend.meta_file.read_bytes())
                finally:
                    self.claude_dir, self.cache_dir, self.backend = (
                        old_claude,
                        old_cache,
                        old_backend,
                    )

    def test_rollback_failure_exposes_verified_recovery_backup(self) -> None:
        self.migrate_two_profiles(active="b")
        self.create_live(v2="B_V2")

        def fail_restore(step: str) -> None:
            if step == "restore:config":
                raise OSError("injected restore failure")

        backend = self.switched_backend(fault_hook=fail_restore)
        with patch.object(backend, "_rollback_live", side_effect=OSError("rollback failed")):
            with self.assertRaises(RollbackError) as caught:
                backend.switch("a")

        self.assertTrue(caught.exception.recovery_path.is_dir())
        self.assertTrue(
            (caught.exception.recovery_path / "backup-manifest.json").is_file()
        )

    def test_history_sync_failure_does_not_undo_successful_login_switch(self) -> None:
        self.migrate_two_profiles(active="b")
        self.create_live(v2="B_V2")

        def fail_history():
            raise OSError("history unavailable")

        result = self.switched_backend(history_sync=fail_history).switch("a")

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.history)
        self.assertFalse(result.history.ok)
        config = json.loads((self.claude_dir / "config.json").read_text())
        self.assertEqual("A_V2", config["oauth:tokenCacheV2"])
        self.assertEqual("a", json.loads(self.backend.meta_file.read_text())["active"])

    def test_backend_history_sync_merges_sessions_and_propagates_deletions(self) -> None:
        self.migrate_two_profiles(active="a")
        root = self.claude_dir / "claude-code-sessions" / "workspace"
        account_a = root / "account-a"
        account_b = root / "account-b"
        account_a.mkdir(parents=True, exist_ok=True)
        account_b.mkdir(parents=True, exist_ok=True)
        (account_a / "a.json").write_text('{"account":"a"}', encoding="utf-8")
        (account_b / "b.json").write_text('{"account":"b"}', encoding="utf-8")
        backend = self.switched_backend(process=FakeProcessAdapter(running=False))

        first = backend.sync_histories()

        self.assertTrue(first.ok)
        self.assertTrue((account_a / "b.json").is_file())
        self.assertTrue((account_b / "a.json").is_file())
        (account_b / "a.json").unlink()

        second = backend.sync_histories()

        self.assertTrue(second.ok)
        self.assertFalse((account_a / "a.json").exists())
        self.assertFalse((account_b / "a.json").exists())
        self.assertGreaterEqual(second.removed, 1)

    def test_desktop_backend_has_no_network_client_for_saved_cookies(self) -> None:
        source = inspect.getsource(desktop_backend)
        forbidden_imports = ("urllib", "requests", "http.client", "socket")

        for name in forbidden_imports:
            self.assertNotIn(f"import {name}", source)

    def test_remove_profile_deletes_only_its_retained_generations(self) -> None:
        store = self.cache_dir / "profiles"
        current = store / "a"
        legacy = store / "a.blob"
        generation = store / ".previous" / "a-20260809123456789012"
        similarly_named = store / ".previous" / "a-b-20260809123456789012"
        for path in (current, generation, similarly_named):
            path.mkdir(parents=True, exist_ok=True)
        legacy.write_text("credential", encoding="utf-8")

        with patch.object(app, "STORE_DIR", store):
            app._delete_profile_store("a")

        self.assertFalse(current.exists())
        self.assertFalse(legacy.exists())
        self.assertFalse(generation.exists())
        self.assertTrue(similarly_named.exists())

    def test_rename_then_remove_deletes_retained_generations(self) -> None:
        store = self.cache_dir / "profiles"
        current = store / "a"
        legacy = store / "a.blob"
        generation = store / ".previous" / "a-20260809123456789012"
        similarly_named = store / ".previous" / "a-b-20260809123456789012"
        for path in (current, generation, similarly_named):
            path.mkdir(parents=True, exist_ok=True)
        legacy.write_text("credential", encoding="utf-8")

        with patch.object(app, "STORE_DIR", store):
            app._rename_profile_store("a", "renamed")
            app._delete_profile_store("renamed")

        self.assertFalse((store / "renamed").exists())
        self.assertFalse((store / "renamed.blob").exists())
        self.assertFalse(
            (store / ".previous" / "renamed-20260809123456789012").exists()
        )
        self.assertFalse(current.exists())
        self.assertFalse(legacy.exists())
        self.assertFalse(generation.exists())
        self.assertTrue(similarly_named.exists())

    def test_default_cookie_decoder_strips_chromium_host_hash_prefix(self) -> None:
        backend = DesktopBackend(claude_dir=self.claude_dir, cache_dir=self.cache_dir)
        backend._decrypt_chromium = lambda _value: bytes(range(32)) + b"session-key"

        self.assertEqual("session-key", backend._decrypt_cookie(b"encrypted"))


if __name__ == "__main__":
    unittest.main()
