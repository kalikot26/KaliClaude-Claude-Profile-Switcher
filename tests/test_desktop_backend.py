from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
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

    def test_cached_proof_is_consulted_but_new_version_requires_startup_record(self) -> None:
        self.create_profile("alpha", "uuid-a", "org-a")
        self.assertTrue(self.backend.launch_active().ok)
        self.platform.pids = []
        self.platform.startup_record = False

        cached = self.backend.launch_active()

        self.assertTrue(cached.ok)
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
