from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gui.desktop_backend as desktop_backend
from gui.desktop_backend import DesktopBackend, DesktopBackendError, ProcessDetectionError, ProfileState


def file_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class FakePlatform:
    def __init__(
        self,
        *,
        running: bool = False,
        version: str = "1.0.0",
        launch_fails: bool = False,
        ignored_environment: bool = False,
        startup_record: bool = True,
        unknown_external: bool = False,
        detection_error: bool = False,
    ) -> None:
        self.pids = [101] if running else []
        self.version = version
        self.launch_fails = launch_fails
        self.ignored_environment = ignored_environment
        self.startup_record = startup_record
        self.unknown_external = unknown_external
        self.detection_error = detection_error
        self.launches: list[tuple[dict, dict[str, str]]] = []
        self.closed: list[int] = []

    def resolve_executable(self) -> dict[str, str]:
        return {
            "path": r"C:\\test\\AnthropicClaude\\app-" + self.version + r"\\Claude.exe",
            "version": self.version,
            "kind": "squirrel",
        }

    def desktop_pids(self) -> list[int]:
        if self.detection_error:
            raise OSError("test detection failure")
        return list(self.pids)

    def unknown_desktop_pids(self) -> list[int]:
        return [999] if self.unknown_external else []

    def request_close(self, pids: list[int]) -> None:
        self.closed.extend(pids)
        self.pids = []

    def wait_stopped(self, _timeout: float) -> bool:
        return not self.pids

    def force_stop(self, pids: list[int]) -> None:
        self.pids = [pid for pid in self.pids if pid not in pids]

    def launch(self, executable: dict, env: dict[str, str]) -> None:
        self.launches.append((executable, dict(env)))
        if self.launch_fails:
            raise OSError("test launch failure")
        self._complete_launch(env)

    def _complete_launch(self, env: dict[str, str]) -> None:
        self.pids = [303]
        root = Path(env["CLAUDE_USER_DATA_DIR"])
        if self.ignored_environment:
            root = root.parent / "ignored-by-desktop"
        log = root / "Logs" / "main.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        lines = ["account active"]
        if self.startup_record:
            lines.insert(0, "startup complete")
        with log.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


class DelayedPlatform(FakePlatform):
    """Starts only after the backend has observed one empty post-launch poll."""

    def __init__(self) -> None:
        super().__init__()
        self._pending_environment: dict[str, str] | None = None
        self._post_launch_polls = 0

    def launch(self, executable: dict, env: dict[str, str]) -> None:
        self.launches.append((executable, dict(env)))
        self._pending_environment = dict(env)

    def desktop_pids(self) -> list[int]:
        if self._pending_environment is not None:
            self._post_launch_polls += 1
            if self._post_launch_polls >= 2:
                environment = self._pending_environment
                self._pending_environment = None
                self._complete_launch(environment)
        return super().desktop_pids()


class NoClassificationPlatform:
    def desktop_pids(self) -> list[int]:
        return []


class IsolatedDesktopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.default_root = root / "Claude"
        self.cache = root / "cache"
        self.platform = FakePlatform()
        self.backend = DesktopBackend(
            claude_dir=self.default_root,
            cache_dir=self.cache,
            process_adapter=self.platform,
            launch_timeout=0.02,
            launch_poll_interval=0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed_root(self, root: Path, *, account_uuid: str, organization: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.json").write_text(
            json.dumps(
                {
                    "lastKnownAccountUuid": account_uuid,
                    "oauth:tokenCacheV2": json.dumps({f"org:{organization}:scope": {"token": "opaque"}}),
                }
            ),
            encoding="utf-8",
        )
        for relative, value in {
            "Network/Cookies": b"cookies-" + account_uuid.encode(),
            "Trusted Devices/device.json": b"trusted-" + account_uuid.encode(),
            "Partitions/partition.txt": b"partition-" + account_uuid.encode(),
            "Local State": b"local-state-" + account_uuid.encode(),
        }.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)

    def create_profile(self, name: str, account_uuid: str, organization: str):
        pending = self.backend.begin_new_login()
        self.seed_root(pending.user_data_dir, account_uuid=account_uuid, organization=organization)
        return self.backend.finalize_current(name, name.title(), "")

    def meta(self) -> dict:
        return json.loads(self.backend.meta_file.read_text(encoding="utf-8"))

    def test_exposes_schema_three_isolated_public_api(self) -> None:
        self.assertEqual(3, desktop_backend.MANIFEST_SCHEMA)
        self.assertEqual("isolated", desktop_backend.StorageMode.ISOLATED.value)
        self.assertEqual("needs_validation", ProfileState.NEEDS_VALIDATION.value)
        for method in (
            "begin_new_login",
            "finalize_current",
            "verify_profile",
            "switch",
            "launch_active",
            "active_user_data_dir",
        ):
            self.assertTrue(hasattr(self.backend, method), method)

    def test_config_account_uuid_remains_authoritative_when_oauth_org_differs(self) -> None:
        profile = self.create_profile("alpha", "account-uuid-a", "organization-b")
        (self.backend.active_user_data_dir() / "Network" / "Cookies").write_bytes(b"session-for-account-b")

        self.assertEqual(
            hashlib.sha256(b"account-uuid-a").hexdigest(), profile.account_id_hash
        )
        org_hashes = self.backend._oauth_organization_hashes(
            json.loads((self.backend.active_user_data_dir() / "config.json").read_text())
        )
        self.assertEqual([hashlib.sha256(b"organization-b").hexdigest()], org_hashes)
        self.assertNotIn(profile.account_id_hash, org_hashes)

    def test_two_isolated_roots_keep_full_distinct_desktop_data(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.create_profile("beta", "uuid-b", "org-b")
        alpha = self.cache / "desktop-data" / "alpha"
        beta = self.cache / "desktop-data" / "beta"

        for relative in ("config.json", "Network/Cookies", "Trusted Devices/device.json", "Partitions/partition.txt", "Local State"):
            self.assertNotEqual((alpha / relative).read_bytes(), (beta / relative).read_bytes())
        self.assertEqual("beta", self.meta()["desktop_active"])
        self.assertNotIn("active", self.meta())

    def test_switch_copies_no_profile_storage_and_direct_launches_target(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.create_profile("beta", "uuid-b", "org-b")
        alpha = self.cache / "desktop-data" / "alpha"
        beta = self.cache / "desktop-data" / "beta"
        alpha_before = file_digest(alpha)
        beta_before = file_digest(beta)
        self.platform.pids = [101]

        result = self.backend.switch("alpha")

        self.assertTrue(result.ok)
        self.assertEqual("alpha", self.meta()["desktop_active"])
        alpha_after = file_digest(alpha)
        self.assertEqual(
            alpha_before,
            {relative: digest for relative, digest in alpha_after.items() if relative != "Logs/main.log"},
        )
        self.assertEqual(beta_before, file_digest(beta))
        _executable, env = self.platform.launches[-1]
        self.assertEqual(str(alpha), env["CLAUDE_USER_DATA_DIR"])
        self.assertFalse(any("explorer" in str(call).lower() for call in self.platform.launches))

    def test_ignored_environment_fails_closed_without_metadata_commit(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.create_profile("beta", "uuid-b", "org-b")
        self.platform.pids = [101]
        self.platform.ignored_environment = True
        before = self.backend.meta_file.read_bytes()

        result = self.backend.switch("alpha")

        self.assertFalse(result.ok)
        self.assertEqual(before, self.backend.meta_file.read_bytes())
        self.assertEqual("beta", self.meta()["desktop_active"])

    def test_launch_failure_and_detection_errors_fail_closed(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        before = self.backend.meta_file.read_bytes()
        self.platform.launch_fails = True

        result = self.backend.launch_active()

        self.assertFalse(result.ok)
        self.assertEqual(before, self.backend.meta_file.read_bytes())
        self.platform.launch_fails = False
        self.platform.detection_error = True
        with self.assertRaises(ProcessDetectionError):
            self.backend.switch("alpha")
        self.assertEqual(before, self.backend.meta_file.read_bytes())

    def test_unknown_external_launch_fails_closed(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.platform.unknown_external = True
        before = self.backend.meta_file.read_bytes()

        with self.assertRaises(DesktopBackendError):
            self.backend.switch("alpha")

        self.assertEqual(before, self.backend.meta_file.read_bytes())

    def test_begin_new_login_does_not_modify_current_root(self) -> None:
        self.seed_root(self.default_root, account_uuid="uuid-default", organization="org-default")
        before = file_digest(self.default_root)

        pending = self.backend.begin_new_login()

        self.assertTrue(pending.user_data_dir.is_dir())
        self.assertNotEqual(self.default_root, pending.user_data_dir)
        self.assertEqual(before, file_digest(self.default_root))

    def test_begin_new_login_always_creates_a_fresh_pending_root(self) -> None:
        first = self.backend.begin_new_login()
        (first.user_data_dir / "stale-login-marker").write_text("fixture", encoding="utf-8")

        second = self.backend.begin_new_login()

        self.assertNotEqual(first.name, second.name)
        self.assertTrue((first.user_data_dir / "stale-login-marker").is_file())
        self.assertFalse((second.user_data_dir / "stale-login-marker").exists())

    def test_verify_profile_persists_a_corrupt_config_identity(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        config = self.backend.active_user_data_dir() / "config.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["lastKnownAccountUuid"] = "uuid-b"
        config.write_text(json.dumps(payload), encoding="utf-8")

        verified = self.backend.verify_profile("alpha")

        self.assertEqual(ProfileState.CORRUPT, verified.state)
        self.assertEqual(ProfileState.CORRUPT, self.backend.list_profiles()[0].state)

    def test_metadata_commits_only_after_verified_running_switch(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.create_profile("beta", "uuid-b", "org-b")
        self.platform.pids = [101]
        self.platform.startup_record = False
        before = self.backend.meta_file.read_bytes()

        result = self.backend.switch("alpha")

        self.assertFalse(result.ok)
        self.assertEqual(before, self.backend.meta_file.read_bytes())
        self.assertEqual("beta", self.meta()["desktop_active"])

    def test_version_change_invalidates_cached_launch_proof(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.assertTrue(self.backend.launch_active().ok)
        proof_keys = set(self.meta()["launch_proofs"])
        self.platform.version = "2.0.0"
        self.platform.startup_record = False

        result = self.backend.launch_active()

        self.assertFalse(result.ok)
        self.assertEqual(proof_keys, set(self.meta()["launch_proofs"]))

    def test_cached_proof_still_requires_a_new_startup_record(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.assertTrue(self.backend.launch_active().ok)
        self.platform.pids = []
        self.platform.startup_record = False

        cached = self.backend.launch_active()

        self.assertFalse(cached.ok)
        self.platform.pids = []
        self.platform.version = "2.0.0"
        invalidated = self.backend.launch_active()
        self.assertFalse(invalidated.ok)

    def test_cached_proof_does_not_allow_a_later_ignored_environment(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.assertTrue(self.backend.launch_active().ok)
        self.platform.pids = []
        self.platform.ignored_environment = True

        result = self.backend.launch_active()

        self.assertFalse(result.ok)

    def test_launch_waits_for_async_process_and_log_startup(self) -> None:
        delayed = DelayedPlatform()
        backend = DesktopBackend(
            claude_dir=self.default_root,
            cache_dir=self.cache,
            process_adapter=delayed,
            launch_timeout=0.5,
            launch_poll_interval=0.001,
        )
        self.backend = backend
        self.create_profile("alpha", "uuid-a", "org-a")

        result = backend.launch_active()

        self.assertTrue(result.ok)
        self.assertGreaterEqual(delayed._post_launch_polls, 2)

    def test_failed_target_launch_stops_it_before_relaunching_previous_profile(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.create_profile("beta", "uuid-b", "org-b")
        self.platform.pids = [101]
        self.platform.ignored_environment = True

        result = self.backend.switch("alpha")

        self.assertFalse(result.ok)
        self.assertEqual(2, len(self.platform.launches))
        self.assertGreaterEqual(self.platform.closed.count(303), 1)
        self.assertEqual(
            str(self.cache / "desktop-data" / "beta"),
            self.platform.launches[-1][1]["CLAUDE_USER_DATA_DIR"],
        )

    def test_default_storage_mode_launches_from_the_default_root(self) -> None:
        self.seed_root(self.default_root, account_uuid="uuid-default", organization="org-default")
        meta = self.backend._load_meta()
        meta["profiles"]["default"] = {
            "label": "Default",
            "storage_mode": desktop_backend.StorageMode.DEFAULT.value,
            "state": ProfileState.NEEDS_VALIDATION.value,
            "account_id_sha256": hashlib.sha256(b"uuid-default").hexdigest(),
        }
        meta["desktop_active"] = "default"
        self.backend._save_meta(meta)

        result = self.backend.launch_active()

        self.assertTrue(result.ok)
        self.assertEqual(self.default_root, self.backend.active_user_data_dir())
        self.assertEqual(str(self.default_root), self.platform.launches[-1][1]["CLAUDE_USER_DATA_DIR"])

    def test_missing_external_launch_classifier_fails_closed(self) -> None:
        backend = DesktopBackend(
            claude_dir=self.default_root,
            cache_dir=self.cache,
            process_adapter=NoClassificationPlatform(),
        )

        with self.assertRaises(ProcessDetectionError):
            backend.desktop_pids()

    def _write_history_log(self, root: Path, workspace: str, account: str) -> None:
        log = root / "Logs" / "main.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            f"persisted sessions from C:\\fixture\\claude-code-sessions\\{workspace}\\{account}\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_history(root: Path, workspace: str, account: str, name: str, value: str) -> Path:
        path = root / "claude-code-sessions" / workspace / account / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def test_history_sync_closes_desktop_and_merges_only_managed_roots(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.create_profile("beta", "uuid-b", "org-b")
        alpha = self.cache / "desktop-data" / "alpha"
        beta = self.cache / "desktop-data" / "beta"
        self._write_history_log(alpha, "workspace", "uuid-a")
        self._write_history_log(beta, "workspace", "uuid-b")
        self._write_history(alpha, "workspace", "uuid-a", "alpha.json", "alpha")
        self._write_history(beta, "workspace", "uuid-b", "beta.json", "beta")
        self.platform.pids = [101]

        report = self.backend.sync_histories()

        self.assertTrue(report.ok, report.message)
        self.assertEqual([], self.platform.pids)
        self.assertEqual([101], self.platform.closed)
        for root, account in ((alpha, "uuid-a"), (beta, "uuid-b")):
            folder = root / "claude-code-sessions" / "workspace" / account
            self.assertEqual({"alpha.json", "beta.json"}, {path.name for path in folder.glob("*.json")})

    def test_history_sync_rejects_encoded_traversal_segments(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        alpha = self.cache / "desktop-data" / "alpha"
        self._write_history_log(alpha, "%2e%2e", "uuid-a")
        outside = self.cache / "desktop-data" / "outside.json"
        outside.write_text("unchanged", encoding="utf-8")

        report = self.backend.sync_histories()

        self.assertFalse(report.ok)
        self.assertIn("unsafe", report.message.lower())
        self.assertEqual("unchanged", outside.read_text(encoding="utf-8"))

    def test_history_deletion_is_scoped_to_explicit_managed_account_directories(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.create_profile("beta", "uuid-b", "org-b")
        alpha = self.cache / "desktop-data" / "alpha"
        beta = self.cache / "desktop-data" / "beta"
        for root, account in ((alpha, "uuid-a"), (beta, "uuid-b")):
            self._write_history_log(root, "workspace", account)
        source = self._write_history(alpha, "workspace", "uuid-a", "removed.json", "managed")
        self.assertTrue(self.backend.sync_histories().ok)
        unrelated = beta / "claude-code-sessions" / "unmanaged" / "other-account" / "removed.json"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("unmanaged", encoding="utf-8")
        source.unlink()

        report = self.backend.sync_histories()

        self.assertTrue(report.ok, report.message)
        self.assertGreaterEqual(report.removed, 1)
        self.assertTrue(unrelated.is_file())
        self.assertEqual("unmanaged", unrelated.read_text(encoding="utf-8"))

    def test_history_failure_is_nonfatal_to_stopped_profile_switch(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.create_profile("beta", "uuid-b", "org-b")
        self.backend._history_sync = lambda: (_ for _ in ()).throw(OSError("fixture history failure"))

        result = self.backend.switch("alpha")

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.history)
        self.assertFalse(result.history.ok)
        self.assertEqual("alpha", self.meta()["desktop_active"])


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.default_root = root / "Claude"
        self.cache = root / "cache"
        self.open_databases: list[sqlite3.Connection] = []
        self.backend = DesktopBackend(
            claude_dir=self.default_root,
            cache_dir=self.cache,
            process_adapter=FakePlatform(),
            oauth_decoder=self.decode_oauth,
            cookie_decoder=self.decode_cookie,
        )

    def tearDown(self) -> None:
        for database in self.open_databases:
            database.close()
        self.temp.cleanup()

    @staticmethod
    def decode_oauth(blob: str) -> dict | None:
        if blob == "ENCRYPTED_EMPTY_V1":
            return {}
        account = blob.split("_", 1)[0].lower()
        if account not in {"a", "b", "old"}:
            return None
        return {f"organization:oauth-{account}:scope": {"token": "opaque"}}

    @staticmethod
    def decode_cookie(blob: bytes) -> str | None:
        return "locally-decrypted-session" if blob.startswith(b"v10\x00") else None

    @staticmethod
    def create_leveldb(root: Path, marker: bytes = b"records") -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "CURRENT").write_text("MANIFEST-000001\n", encoding="ascii")
        (root / "MANIFEST-000001").write_bytes(b"manifest")
        (root / "000003.log").write_bytes(marker)

    def create_cookie_database(
        self,
        root: Path,
        *,
        cookie: bytes = b"v10\x00encrypted-session",
        row_in_wal: bool = False,
    ) -> None:
        cookies = root / "Network" / "Cookies"
        cookies.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(cookies)
        if row_in_wal:
            self.assertEqual("wal", database.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        database.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)"
        )
        database.commit()
        if row_in_wal:
            database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        database.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".claude.ai", "sessionKey", cookie),
        )
        database.commit()
        if row_in_wal:
            self.assertTrue((cookies.parent / "Cookies-wal").is_file())
            self.assertTrue((cookies.parent / "Cookies-shm").is_file())
            self.open_databases.append(database)
        else:
            database.close()

    def create_generation(
        self,
        root: Path,
        *,
        v1: str,
        v2: str,
        marker: bytes = b"generation",
        cookie: bytes = b"v10\x00encrypted-session",
        row_in_wal: bool = False,
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "oauth-v1.blob").write_text(v1, encoding="utf-8")
        (root / "oauth-v2.blob").write_text(v2, encoding="utf-8")
        session = root / "session"
        self.create_leveldb(session / "Session Storage", marker)
        self.create_leveldb(session / "Local Storage" / "leveldb", marker)
        self.create_leveldb(
            session / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb",
            marker,
        )
        self.create_cookie_database(session, cookie=cookie, row_in_wal=row_in_wal)
        return root

    @staticmethod
    def manifest_artifacts(root: Path) -> dict[str, dict[str, object]]:
        return {
            path.relative_to(root).as_posix(): {
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and path.name != "backup-manifest.json"
            and not path.name.endswith(".tmp")
        }

    def create_session_backup(
        self,
        name: str,
        *,
        account_uuid: str,
        v1: str,
        v2: str,
        partition: bytes = b"partition-a",
    ) -> Path:
        backup = self.cache / "backups" / name
        backup.mkdir(parents=True)
        config = {
            "lastKnownAccountUuid": account_uuid,
            "oauth:tokenCache": v1,
            "oauth:tokenCacheV2": v2,
            "unrelated-setting": "preserved-verbatim",
        }
        (backup / "config.json").write_text(
            json.dumps(config, separators=(",", ":")), encoding="utf-8"
        )
        (backup / "Local State").write_bytes(b'{"os_crypt":{"encrypted_key":"fixture"}}')
        (backup / "Partitions").mkdir()
        (backup / "Partitions" / "account-bound.bin").write_bytes(partition)
        (backup / "Trusted Devices").mkdir()
        (backup / "Trusted Devices" / "device.json").write_bytes(b"trusted-device")
        manifest = {
            "schema": 1,
            "presence": {"config.json": True},
            "artifacts": self.manifest_artifacts(backup),
        }
        (backup / "backup-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return backup

    def write_legacy_meta(self, profiles: dict[str, dict], *, active: str | None = None) -> None:
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / "meta.json").write_text(
            json.dumps({"schema": 2, "active": active, "aumid": "legacy", "profiles": profiles}),
            encoding="utf-8",
        )

    def seed_default(self, *, account_uuid: str, v1: str, v2: str) -> None:
        self.default_root.mkdir(parents=True, exist_ok=True)
        (self.default_root / "config.json").write_text(
            json.dumps(
                {
                    "lastKnownAccountUuid": account_uuid,
                    "oauth:tokenCache": v1,
                    "oauth:tokenCacheV2": v2,
                }
            ),
            encoding="utf-8",
        )
        (self.default_root / "Local State").write_bytes(b"live-local-state")
        (self.default_root / "claude_desktop_config.json").write_bytes(b'{"shared":true}')

    def test_selects_newest_healthy_retained_generation_and_seeds_complete_root(self) -> None:
        current = self.create_generation(
            self.cache / "profiles" / "alpha",
            v1="ENCRYPTED_EMPTY_V1",
            v2="A_V2",
            marker=b"degraded-current",
        )
        older = self.create_generation(
            self.cache / "profiles" / ".previous" / "alpha-20260801000000000000",
            v1="OLD_V1",
            v2="OLD_V2",
            marker=b"older-healthy",
        )
        newest = self.create_generation(
            self.cache / "profiles" / ".previous" / "alpha-20260802000000000000",
            v1="A_V1",
            v2="A_V2",
            marker=b"newest-healthy",
        )
        backup = self.create_session_backup(
            "session-20260802", account_uuid="opaque-account-uuid", v1="A_V1", v2="A_V2"
        )
        self.default_root.mkdir(parents=True)
        (self.default_root / "claude_desktop_config.json").write_bytes(b'{"shared":true}')
        legacy_before = {
            "current": file_digest(current),
            "older": file_digest(older),
            "newest": file_digest(newest),
            "backup": file_digest(backup),
        }
        self.write_legacy_meta({"alpha": {"label": "Alpha", "note": "legacy-note"}})

        report = self.backend.audit_and_migrate()

        isolated = self.cache / "desktop-data" / "alpha"
        config_bytes = (isolated / "config.json").read_bytes()
        self.assertEqual((backup / "config.json").read_bytes(), config_bytes)
        config = json.loads(config_bytes)
        self.assertEqual("A_V1", config["oauth:tokenCache"])
        self.assertEqual("A_V2", config["oauth:tokenCacheV2"])
        self.assertNotEqual(config["oauth:tokenCache"], config["oauth:tokenCacheV2"])
        self.assertEqual(
            b"newest-healthy", (isolated / "Local Storage" / "leveldb" / "000003.log").read_bytes()
        )
        self.assertEqual((backup / "Local State").read_bytes(), (isolated / "Local State").read_bytes())
        self.assertEqual(
            (backup / "Partitions" / "account-bound.bin").read_bytes(),
            (isolated / "Partitions" / "account-bound.bin").read_bytes(),
        )
        self.assertEqual(b'{"shared":true}', (isolated / "claude_desktop_config.json").read_bytes())
        entry = json.loads(self.backend.meta_file.read_text())["profiles"]["alpha"]
        self.assertEqual("needs_validation", entry["state"])
        self.assertEqual(
            hashlib.sha256(b"opaque-account-uuid").hexdigest(), entry["account_id_sha256"]
        )
        self.assertEqual(["alpha"], report.migrated)
        self.assertNotIn("opaque-account-uuid", json.dumps(report.__dict__, default=str))
        self.assertEqual(legacy_before["current"], file_digest(current))
        self.assertEqual(legacy_before["older"], file_digest(older))
        self.assertEqual(legacy_before["newest"], file_digest(newest))
        self.assertEqual(legacy_before["backup"], file_digest(backup))

    def test_exact_verified_full_config_match_is_mandatory_or_profile_needs_relogin(self) -> None:
        self.create_generation(self.cache / "profiles" / "alpha", v1="A_V1", v2="A_V2")
        backup = self.create_session_backup(
            "session-mismatch", account_uuid="opaque-account", v1="OLD_V1", v2="A_V2"
        )
        # Corrupting a manifest-covered file also proves that an unverified backup is rejected.
        (backup / "Local State").write_bytes(b"changed-after-manifest")
        self.write_legacy_meta({"alpha": {"label": "Alpha"}})

        report = self.backend.audit_and_migrate()

        isolated = self.cache / "desktop-data" / "alpha"
        self.assertEqual(["alpha"], report.needs_relogin)
        self.assertFalse((isolated / "config.json").exists())
        self.assertFalse((isolated / "Network" / "Cookies").exists())
        entry = json.loads(self.backend.meta_file.read_text())["profiles"]["alpha"]
        self.assertEqual("needs_relogin", entry["state"])
        self.assertNotIn("account_id_sha256", entry)

    def test_default_mapping_uses_unique_live_identity_and_ignores_stale_active_metadata(self) -> None:
        self.create_generation(self.cache / "profiles" / "alpha", v1="A_V1", v2="A_V2")
        self.create_generation(self.cache / "profiles" / "beta", v1="B_V1", v2="B_V2")
        self.create_session_backup(
            "session-alpha", account_uuid="uuid-alpha", v1="A_V1", v2="A_V2"
        )
        self.seed_default(account_uuid="uuid-beta", v1="B_V1", v2="B_V2")
        default_before = file_digest(self.default_root)
        self.write_legacy_meta(
            {"alpha": {"label": "Alpha"}, "beta": {"label": "Beta"}}, active="alpha"
        )

        self.backend.audit_and_migrate()

        meta = json.loads(self.backend.meta_file.read_text())
        self.assertEqual("beta", meta["desktop_active"])
        self.assertEqual("default", meta["profiles"]["beta"]["storage_mode"])
        self.assertEqual("isolated", meta["profiles"]["alpha"]["storage_mode"])
        self.assertEqual(
            hashlib.sha256(b"uuid-beta").hexdigest(), meta["profiles"]["beta"]["account_id_sha256"]
        )
        self.assertEqual(default_before, file_digest(self.default_root))

    def test_migration_is_idempotent_and_creates_one_pre_migration_backup(self) -> None:
        legacy = self.create_generation(
            self.cache / "profiles" / "alpha", v1="A_V1", v2="A_V2"
        )
        self.create_session_backup(
            "session-alpha", account_uuid="uuid-alpha", v1="A_V1", v2="A_V2"
        )
        self.write_legacy_meta({"alpha": {"label": "Alpha"}})
        legacy_before = file_digest(legacy)

        first = self.backend.audit_and_migrate()
        meta_before = self.backend.meta_file.read_bytes()
        isolated_before = file_digest(self.cache / "desktop-data" / "alpha")
        second = self.backend.audit_and_migrate()

        migration_backups = list((self.cache / "backups").glob("operational-migration-*"))
        self.assertIsNotNone(first.backup)
        self.assertIsNone(second.backup)
        self.assertEqual(1, len(migration_backups))
        self.assertEqual(meta_before, self.backend.meta_file.read_bytes())
        self.assertEqual(isolated_before, file_digest(self.cache / "desktop-data" / "alpha"))
        self.assertEqual(legacy_before, file_digest(legacy))
        self.assertEqual(["alpha"], second.unchanged)

    def test_restart_reuses_promoted_root_after_crash_before_schema_three_commit(self) -> None:
        self.create_generation(self.cache / "profiles" / "alpha", v1="A_V1", v2="A_V2")
        self.create_session_backup(
            "session-alpha", account_uuid="uuid-alpha", v1="A_V1", v2="A_V2"
        )
        self.write_legacy_meta({"alpha": {"label": "Alpha"}})

        def crash_after_promotion(step: str) -> None:
            if step == "migration:promoted:alpha":
                raise RuntimeError("fixture crash")

        self.backend._fault_hook = crash_after_promotion
        with self.assertRaisesRegex(RuntimeError, "fixture crash"):
            self.backend.audit_and_migrate()
        promoted = self.cache / "desktop-data" / "alpha"
        promoted_before = file_digest(promoted)

        restarted = DesktopBackend(
            claude_dir=self.default_root,
            cache_dir=self.cache,
            process_adapter=FakePlatform(),
            oauth_decoder=self.decode_oauth,
            cookie_decoder=self.decode_cookie,
        )
        report = restarted.audit_and_migrate()

        self.assertEqual(["alpha"], report.migrated)
        self.assertEqual(promoted_before, file_digest(promoted))
        quarantine = self.cache / "desktop-data" / ".quarantine"
        self.assertEqual([], list(quarantine.glob("alpha-*")) if quarantine.exists() else [])

    def test_operational_backup_cap_preserves_all_legacy_session_backups(self) -> None:
        backups = self.cache / "backups"
        for index in range(5):
            operational = backups / f"operational-history-{index}"
            operational.mkdir(parents=True)
            (operational / ".kalikot-operational-backup").write_text(str(index), encoding="ascii")
        legacy_backups = []
        for index in range(7):
            backup = backups / f"session-legacy-{index}"
            backup.mkdir(parents=True)
            (backup / "opaque.bin").write_bytes(bytes([index]))
            legacy_backups.append((backup, file_digest(backup)))
        self.create_generation(self.cache / "profiles" / "alpha", v1="A_V1", v2="A_V2")
        self.write_legacy_meta({"alpha": {"label": "Alpha"}})

        self.backend.audit_and_migrate()

        managed = [
            path
            for path in backups.glob("operational-*")
            if (path / ".kalikot-operational-backup").is_file()
        ]
        self.assertEqual(5, len(managed))
        self.assertFalse((backups / "operational-history-0").exists())
        self.assertEqual(1, len(list(backups.glob("operational-migration-*"))))
        for backup, digest in legacy_backups:
            self.assertEqual(digest, file_digest(backup))

    def test_interrupted_temp_files_are_ignored_and_displaced_staging_is_quarantined(self) -> None:
        generation = self.create_generation(
            self.cache / "profiles" / "alpha", v1="A_V1", v2="A_V2"
        )
        (generation / "session" / "Network" / "Cookies.tmp").write_bytes(b"interrupted-secret")
        backup = self.create_session_backup(
            "session-alpha", account_uuid="uuid-alpha", v1="A_V1", v2="A_V2"
        )
        (backup / "config.json.tmp").write_bytes(b"interrupted-config")
        displaced = self.cache / "desktop-data" / "alpha"
        displaced.mkdir(parents=True)
        (displaced / "staging-marker").write_bytes(b"must-survive")
        self.write_legacy_meta({"alpha": {"label": "Alpha"}})

        self.backend.audit_and_migrate()

        self.assertFalse((self.cache / "desktop-data" / "alpha" / "Cookies.tmp").exists())
        self.assertFalse((self.cache / "desktop-data" / "alpha" / "config.json.tmp").exists())
        quarantined = list((self.cache / "desktop-data" / ".quarantine").glob("alpha-*"))
        self.assertEqual(1, len(quarantined))
        self.assertEqual(b"must-survive", (quarantined[0] / "staging-marker").read_bytes())
        self.assertEqual(b"interrupted-secret", (generation / "session" / "Network" / "Cookies.tmp").read_bytes())
        self.assertEqual(b"interrupted-config", (backup / "config.json.tmp").read_bytes())

    def test_cookie_validation_materializes_real_wal_on_disposable_read_write_copy(self) -> None:
        generation = self.create_generation(
            self.cache / "profiles" / "alpha",
            v1="A_V1",
            v2="A_V2",
            row_in_wal=True,
        )
        self.create_session_backup(
            "session-alpha", account_uuid="uuid-alpha", v1="A_V1", v2="A_V2"
        )
        self.write_legacy_meta({"alpha": {"label": "Alpha"}})
        wal = generation / "session" / "Network" / "Cookies-wal"
        shm = generation / "session" / "Network" / "Cookies-shm"
        sidecars_before = (wal.read_bytes(), shm.read_bytes())

        report = self.backend.audit_and_migrate()

        self.assertEqual(["alpha"], report.migrated)
        with closing(sqlite3.connect(self.cache / "desktop-data" / "alpha" / "Network" / "Cookies")) as db:
            value = db.execute(
                "SELECT encrypted_value FROM cookies WHERE name='sessionKey'"
            ).fetchone()[0]
        self.assertEqual(b"v10\x00encrypted-session", bytes(value))
        self.assertEqual(sidecars_before, (wal.read_bytes(), shm.read_bytes()))

    def test_printable_non_token_cookie_is_rejected_before_decoder(self) -> None:
        self.create_generation(
            self.cache / "profiles" / "alpha",
            v1="A_V1",
            v2="A_V2",
            cookie=b"printable-but-not-an-encrypted-token",
        )
        self.create_session_backup(
            "session-alpha", account_uuid="uuid-alpha", v1="A_V1", v2="A_V2"
        )
        self.write_legacy_meta({"alpha": {"label": "Alpha"}})
        self.backend._cookie_decoder = lambda _blob: "decoder-must-not-make-plaintext-valid"

        report = self.backend.audit_and_migrate()

        self.assertEqual(["alpha"], report.needs_relogin)
        self.assertFalse((self.cache / "desktop-data" / "alpha" / "Network" / "Cookies").exists())

    def test_cookie_recovery_ast_has_no_network_boundary(self) -> None:
        source = Path(desktop_backend.__file__).read_text(encoding="utf-8")
        module = ast.parse(source)
        validator = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "DesktopBackend"
            for node in node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_validate_legacy_cookies"
        )
        forbidden_imports = {"requests", "urllib", "http", "socket", "httpx", "aiohttp"}
        imports = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(module)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")])
        }
        forbidden_calls = {
            node.func.attr
            for node in ast.walk(validator)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        } & {"get", "post", "request", "urlopen", "create_connection"}
        self.assertFalse(imports & forbidden_imports)
        self.assertEqual(set(), forbidden_calls)

    def test_migration_hashes_only_operation_sources_and_not_unrelated_default_files(self) -> None:
        self.create_generation(self.cache / "profiles" / "alpha", v1="A_V1", v2="A_V2")
        self.create_session_backup(
            "session-alpha", account_uuid="uuid-alpha", v1="A_V1", v2="A_V2"
        )
        self.default_root.mkdir(parents=True)
        unrelated = self.default_root / "Logs" / "large-unrelated.log"
        unrelated.parent.mkdir()
        unrelated.write_bytes(b"unrelated")
        self.write_legacy_meta({"alpha": {"label": "Alpha"}})
        hashed: list[Path] = []

        original = desktop_backend._sha256_file

        def record_hash(path: Path) -> str:
            hashed.append(Path(path))
            return original(Path(path))

        with patch.object(desktop_backend, "_sha256_file", side_effect=record_hash):
            self.backend.audit_and_migrate()

        self.assertNotIn(unrelated, hashed)
        self.assertTrue(hashed)

    def test_default_migration_oauth_decoder_accepts_only_nonempty_objects(self) -> None:
        backend = DesktopBackend(
            claude_dir=self.default_root,
            cache_dir=self.cache,
            process_adapter=FakePlatform(),
        )
        backend._decrypt_chromium = lambda encrypted: encrypted

        self.assertEqual({"cache": {"token": "opaque"}}, backend._decrypt_oauth(
            desktop_backend.base64.b64encode(b'{"cache":{"token":"opaque"}}').decode("ascii")
        ))
        self.assertIsNone(backend._decrypt_oauth(
            desktop_backend.base64.b64encode(b"{}").decode("ascii")
        ))

    def test_default_migration_cookie_decoder_strips_chromium_host_hash(self) -> None:
        backend = DesktopBackend(
            claude_dir=self.default_root,
            cache_dir=self.cache,
            process_adapter=FakePlatform(),
        )
        backend._decrypt_chromium = lambda _encrypted: bytes(range(32)) + b"session-key"

        self.assertEqual("session-key", backend._decrypt_cookie(b"encrypted"))

    def test_recovered_root_is_locally_decrypted_against_its_copied_local_state(self) -> None:
        self.create_generation(self.cache / "profiles" / "alpha", v1="A_V1", v2="A_V2")
        self.create_session_backup(
            "session-alpha", account_uuid="uuid-alpha", v1="A_V1", v2="A_V2"
        )
        self.write_legacy_meta({"alpha": {"label": "Alpha"}})
        decoder_roots: list[Path | None] = []

        def tracked_oauth(blob: str) -> dict | None:
            decoder_roots.append(getattr(self.backend, "_decryption_root", None))
            return self.decode_oauth(blob)

        self.backend._oauth_decoder = tracked_oauth

        self.backend.audit_and_migrate()

        self.assertTrue(
            any(
                root is not None
                and root.parent == self.cache / "desktop-data"
                and root.name.startswith(".alpha-migration-")
                for root in decoder_roots
            )
        )


class AppDataResolutionTests(unittest.TestCase):
    def test_default_root_uses_appdata_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"APPDATA": directory}, clear=False
        ):
            backend = DesktopBackend(cache_dir=Path(directory) / "cache", process_adapter=FakePlatform())

        self.assertEqual(Path(directory) / "Claude", backend.claude_dir)


class WindowsExecutableResolutionTests(unittest.TestCase):
    def test_squirrel_resolution_prefers_the_newest_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory) / "AnthropicClaude"
            for version in ("1.9.0", "1.10.0"):
                executable = install / f"app-{version}" / "Claude.exe"
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"fixture")
            adapter = desktop_backend.WindowsDesktopProcessAdapter()
            with patch.dict("os.environ", {"LOCALAPPDATA": directory}, clear=False), patch.object(
                adapter, "_valid_product", return_value=True
            ):
                executable = adapter.resolve_executable()

        self.assertEqual("1.10.0", executable.version)

    def test_process_detection_never_targets_claude_code(self) -> None:
        desktop = Path(r"C:\\Users\\fixture\\AppData\\Local\\AnthropicClaude\\app-1.0.0\\Claude.exe")
        code = Path(r"C:\\Users\\fixture\\AppData\\Roaming\\Claude\\claude-code\\Claude.exe")
        adapter = desktop_backend.WindowsDesktopProcessAdapter()
        rows = json.dumps(
            [
                {"ProcessId": 10, "ExecutablePath": str(desktop)},
                {"ProcessId": 20, "ExecutablePath": str(code)},
            ]
        )
        with patch.object(desktop_backend.os, "name", "nt"), patch.object(
            adapter, "_run", return_value=rows
        ), patch.object(adapter, "_verified_squirrel", return_value=[
            desktop_backend.ExecutableSpec(desktop, "1.0.0", "squirrel")
        ]), patch.object(adapter, "_verified_msix", return_value=[]):
            pids = adapter.desktop_pids()

        self.assertEqual([10], pids)

    def test_process_detection_rejects_custom_claude_code_even_with_matching_metadata(self) -> None:
        desktop = Path(r"C:\\Users\\fixture\\AppData\\Local\\AnthropicClaude\\app-1.0.0\\Claude.exe")
        custom_code = Path(r"C:\\Tools\\ClaudeCode\\Claude.exe")
        adapter = desktop_backend.WindowsDesktopProcessAdapter()
        rows = json.dumps(
            [
                {"ProcessId": 10, "ExecutablePath": str(desktop)},
                {"ProcessId": 20, "ExecutablePath": str(custom_code)},
            ]
        )
        with patch.object(desktop_backend.os, "name", "nt"), patch.object(
            adapter, "_run", return_value=rows
        ), patch.object(adapter, "_verified_squirrel", return_value=[
            desktop_backend.ExecutableSpec(desktop, "1.0.0", "squirrel")
        ]), patch.object(adapter, "_verified_msix", return_value=[]), patch.object(
            adapter, "_valid_product", return_value=True
        ):
            with self.assertRaises(OSError):
                adapter.desktop_pids()

    def test_untracked_verified_desktop_process_is_unknown_external(self) -> None:
        adapter = desktop_backend.WindowsDesktopProcessAdapter()
        adapter._managed_pids = {10: "managed-start"}
        records = [
            SimpleNamespace(pid=10, parent_pid=0, started="managed-start"),
            SimpleNamespace(pid=20, parent_pid=0, started="external-start"),
        ]
        with patch.object(adapter, "_verified_desktop_processes", return_value=records):
            unknown = adapter.unknown_desktop_pids()

        self.assertEqual([20], unknown)

    def test_managed_electron_child_process_is_not_an_external_launch(self) -> None:
        adapter = desktop_backend.WindowsDesktopProcessAdapter()
        adapter._managed_pids = {10: "managed-start"}
        records = [
            SimpleNamespace(pid=10, parent_pid=0, started="managed-start"),
            SimpleNamespace(pid=11, parent_pid=10, started="child-start"),
        ]
        with patch.object(adapter, "_verified_desktop_processes", return_value=records):
            unknown = adapter.unknown_desktop_pids()

        self.assertEqual([], unknown)

    def test_reused_managed_pid_is_not_trusted_as_external_desktop(self) -> None:
        adapter = desktop_backend.WindowsDesktopProcessAdapter()
        adapter._managed_pids = {10: "old-start"}
        records = [SimpleNamespace(pid=10, parent_pid=0, started="new-start")]
        with patch.object(adapter, "_verified_desktop_processes", return_value=records):
            unknown = adapter.unknown_desktop_pids()

        self.assertEqual([10], unknown)

    def test_managed_launcher_invokes_the_verified_executable_directly(self) -> None:
        adapter = desktop_backend.WindowsDesktopProcessAdapter()
        executable = desktop_backend.ExecutableSpec(Path(r"C:\\fixture\\Claude.exe"), "1.0.0", "squirrel")
        environment = {"CLAUDE_USER_DATA_DIR": r"C:\\fixture\\profile"}
        with patch.object(desktop_backend.subprocess, "Popen") as popen:
            adapter.launch(executable, environment)

        self.assertEqual([str(executable.path)], popen.call_args.args[0])
        self.assertEqual(environment, popen.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
