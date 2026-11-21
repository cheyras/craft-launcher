import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import storytold_install as install


SHA_A = "a" * 40
SHA_B = "b" * 40


def completed(argv=(), returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def fork_node(repo, sha=SHA_A, owner="storytold", is_fork=True):
    return {
        "nameWithOwner": f"cheyras/{repo}",
        "isFork": is_fork,
        "parent": {"name": repo, "owner": {"login": owner}},
        "ref": None if sha is None else {"target": {"oid": sha}},
    }


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.local = Path(self.temp.name) / "local"
        self.root.mkdir()
        self.local.mkdir()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_query_forks_distinguishes_actual_missing_from_network_failure(self):
        missing = {
            "data": {"r0": None},
            "errors": [{"type": "NOT_FOUND", "path": ["r0"], "message": "Could not resolve to a Repository with the name 'cheyras/new-app'."}],
        }
        with mock.patch.object(install, "_run", return_value=completed(returncode=1, stdout=json.dumps(missing))):
            self.assertEqual({"new-app": None}, install.query_forks(["new-app"]))

        with mock.patch.object(
            install,
            "_run",
            return_value=completed(returncode=1, stderr="authentication required"),
        ):
            with self.assertRaisesRegex(install.InstallError, "authentication required"):
                install.query_forks(["new-app"])

        unexplained = {"data": {"r0": None}}
        with mock.patch.object(install, "_run", return_value=completed(stdout=json.dumps(unexplained))):
            with self.assertRaisesRegex(install.InstallError, "unexplained empty"):
                install.query_forks(["new-app"])

    def test_query_forks_accepts_exact_partial_and_rejects_mixed_denied(self):
        payload = {
            "data": {"r0": fork_node("anicraft"), "r1": None},
            "errors": [{
                "type": "NOT_FOUND",
                "path": ["r1"],
                "message": "Could not resolve to a Repository with the name 'cheyras/example'.",
            }],
        }
        with mock.patch.object(install, "_run", return_value=completed(returncode=1, stdout=json.dumps(payload))):
            self.assertEqual(
                {"anicraft": fork_node("anicraft"), "example": None},
                install.query_forks(["anicraft", "example"]),
            )
        payload["errors"].append({"type": "FORBIDDEN", "path": ["r0"], "message": "Resource not accessible"})
        with mock.patch.object(install, "_run", return_value=completed(returncode=1, stdout=json.dumps(payload))):
            with self.assertRaisesRegex(install.InstallError, "Resource not accessible"):
                install.query_forks(["anicraft", "example"])

    def test_status_cli_schema_new_wrong_parent_and_ready(self):
        metadata = {
            "new-app": None,
            "wrong-app": fork_node("wrong-app", owner="somebody"),
            "anicraft": fork_node("anicraft"),
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        (self.root / "anicraft").mkdir()
        with mock.patch.object(install, "query_forks", return_value=metadata), mock.patch.object(
            install, "validate_clone", return_value=self.root / "anicraft"
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = install.main(["--root", str(self.root), "status", "new-app", "wrong-app", "anicraft"])
        self.assertEqual(0, code)
        rows = json.loads(stdout.getvalue())
        self.assertEqual(["new", "error", "ready"], [row["state"] for row in rows])
        self.assertEqual(
            {"repo", "state", "remote_sha", "installed_sha", "detail"},
            set(rows[0]),
        )
        self.assertIsNone(rows[0]["remote_sha"])
        self.assertEqual(SHA_A, rows[2]["remote_sha"])
        self.assertIsNone(rows[2]["installed_sha"])
        self.assertEqual("", stderr.getvalue())

    def test_status_network_failure_is_json_error_not_new(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(install, "query_forks", side_effect=install.InstallError("network down")), contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(stderr):
            code = install.main(["--root", str(self.root), "status", "anicraft"])
        self.assertEqual(0, code)
        rows = json.loads(stdout.getvalue())
        self.assertEqual("error", rows[0]["state"])
        self.assertIn("network down", rows[0]["detail"])
        self.assertEqual("", stderr.getvalue())

    def test_status_preserves_verified_installed_sha_during_github_failure(self):
        version = self.local / "StorytoldForks" / "anicraft" / "versions" / SHA_A
        version.mkdir(parents=True)
        executable = version / "anicraft.exe"
        executable.write_bytes(b"installed")
        digest = install.hashlib.sha256(executable.read_bytes()).hexdigest()
        receipt = {
            "schema": 1,
            "repo": "anicraft",
            "sha": SHA_A,
            "executable": "anicraft.exe",
            "files": ["anicraft.exe"],
            "sha256": {"anicraft.exe": digest},
        }
        (version.parents[1] / "active.json").write_text(json.dumps(receipt), encoding="utf-8")
        with mock.patch.object(install, "query_forks", side_effect=install.InstallError("offline")):
            row = install.status_rows(self.root, ["anicraft"])[0]
        self.assertEqual("error", row["state"])
        self.assertEqual(SHA_A, row["installed_sha"])

    def test_query_rejects_inexact_returned_repository_identity(self):
        node = fork_node("anicraft")
        node["nameWithOwner"] = "cheyras/other"
        with mock.patch.object(
            install, "_run", return_value=completed(stdout=json.dumps({"data": {"r0": node}}))
        ):
            with self.assertRaisesRegex(install.InstallError, "not a fork"):
                install.query_forks(["anicraft"])

    def test_known_fork_without_branch_needs_branch(self):
        with mock.patch.object(install, "query_forks", return_value={"anicraft": fork_node("anicraft", sha=None)}):
            row = install.status_rows(self.root, ["anicraft"])[0]
        self.assertEqual("needs-branch", row["state"])
        self.assertIsNone(row["remote_sha"])

    def test_missing_checkout_is_needs_clone_but_existing_bad_checkout_is_error(self):
        metadata = {"anicraft": fork_node("anicraft")}
        with mock.patch.object(install, "query_forks", return_value=metadata):
            self.assertEqual("needs-clone", install.status_rows(self.root, ["anicraft"])[0]["state"])
        (self.root / "anicraft").mkdir()
        with mock.patch.object(install, "query_forks", return_value=metadata), mock.patch.object(
            install, "validate_clone", side_effect=install.InstallError("wrong origin")
        ):
            row = install.status_rows(self.root, ["anicraft"])[0]
        self.assertEqual("error", row["state"])
        self.assertIn("wrong origin", row["detail"])

    def test_forged_or_stale_receipt_is_an_error_not_ready(self):
        (self.root / "anicraft").mkdir()
        receipt_dir = self.local / "StorytoldForks" / "anicraft"
        receipt_dir.mkdir(parents=True)
        (receipt_dir / "active.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "repo": "anicraft",
                    "sha": SHA_A,
                    "executable": "../../outside.exe",
                    "files": ["anicraft.exe"],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(install, "query_forks", return_value={"anicraft": fork_node("anicraft")}), mock.patch.object(
            install, "validate_clone"
        ):
            row = install.status_rows(self.root, ["anicraft"])[0]
        self.assertEqual("error", row["state"])
        self.assertIn("invalid install receipt", row["detail"])

    def test_package_selection_launcher_and_normal_app(self):
        metadata = {
            "packages": [
                {"name": "artcraft-launcher", "targets": [{"name": "artcraft-launcher", "kind": ["bin"]}]},
                {"name": "artcraft-launcher-mcp", "targets": [{"name": "craft-ai-mcp", "kind": ["bin"]}]},
                {"name": "anicraft", "targets": [{"name": "anicraft", "kind": ["bin"]}]},
                {"name": "anicraft-cli", "targets": [{"name": "anicraft-cli", "kind": ["bin"]}]},
                {"name": "anicraft-worker", "targets": [{"name": "anicraft-worker", "kind": ["bin"]}]},
            ]
        }
        self.assertEqual(
            (["artcraft-launcher", "artcraft-launcher-mcp"], ["artcraft-launcher", "craft-ai-mcp"]),
            install.select_packages(metadata, "craft-launcher"),
        )
        self.assertEqual(
            (["anicraft", "anicraft-cli"], ["anicraft", "anicraft-cli"]),
            install.select_packages(metadata, "anicraft"),
        )
        self.assertEqual(
            (["anicraft", "anicraft-worker", "anicraft-cli"], ["anicraft", "anicraft-worker", "anicraft-cli"]),
            install.select_packages(metadata, "anicraft", ["anicraft", "anicraft-worker"]),
        )
        with self.assertRaisesRegex(install.InstallError, "no verified"):
            install.select_packages(metadata, "photocraft")

    def _fake_build_run(self, fail_cargo=False):
        def run(argv, **kwargs):
            args = [os.fspath(x) for x in argv]
            if args[:2] == ["cargo", "build"]:
                return completed(args, 1 if fail_cargo else 0, stderr="compiler error" if fail_cargo else "")
            if "rev-parse" in args and ("FETCH_HEAD" in args or "HEAD" in args):
                return completed(args, stdout=SHA_B + "\n")
            return completed(args)

        return run

    @staticmethod
    def _metadata(*_args, **_kwargs):
        return {"packages": [{"name": "anicraft", "targets": [{"name": "anicraft", "kind": ["bin"]}]}]}

    @staticmethod
    def _copy(_source, _bin_dir, stage, binaries):
        name = binaries[0] + ".exe"
        (stage / name).write_bytes(b"PE fixture")
        return [name]

    def test_build_activates_exact_verified_commit(self):
        clone = self.root / "anicraft"
        clone.mkdir()
        with mock.patch.object(install, "_is_windows", return_value=True), mock.patch.object(
            install, "validate_clone", return_value=clone
        ), mock.patch.object(install, "_verified_remote_sha", return_value=SHA_B), mock.patch.object(
            install, "_run", side_effect=self._fake_build_run()
        ), mock.patch.object(install, "_load_metadata", side_effect=self._metadata), mock.patch.object(
            install, "_copy_payload", side_effect=self._copy
        ), mock.patch.object(
            install, "_stream_command", return_value=0
        ):
            installed = install.build_repo(self.root, "anicraft")
        self.assertEqual(SHA_B, installed)
        receipt = json.loads((self.local / "StorytoldForks" / "anicraft" / "active.json").read_text(encoding="utf-8"))
        self.assertEqual(SHA_B, receipt["sha"])
        self.assertTrue(
            (self.local / "StorytoldForks" / "anicraft" / "versions" / SHA_B / "anicraft.exe").is_file()
        )

    def test_failed_update_does_not_replace_active_receipt(self):
        app = self.local / "StorytoldForks" / "anicraft"
        old_version = app / "versions" / SHA_A
        old_version.mkdir(parents=True)
        (old_version / "anicraft.exe").write_bytes(b"old")
        old_receipt = {
            "schema": 1,
            "repo": "anicraft",
            "sha": SHA_A,
            "executable": "anicraft.exe",
            "files": ["anicraft.exe"],
        }
        (app / "active.json").write_text(json.dumps(old_receipt), encoding="utf-8")
        clone = self.root / "anicraft"
        clone.mkdir()
        with mock.patch.object(install, "_is_windows", return_value=True), mock.patch.object(
            install, "validate_clone", return_value=clone
        ), mock.patch.object(install, "_verified_remote_sha", return_value=SHA_B), mock.patch.object(
            install, "_run", side_effect=self._fake_build_run(fail_cargo=True)
        ), mock.patch.object(install, "_load_metadata", side_effect=self._metadata), mock.patch.object(
            install, "_stream_command", return_value=1
        ):
            with self.assertRaisesRegex(install.InstallError, "build log"):
                install.build_repo(self.root, "anicraft")
        self.assertEqual(old_receipt, json.loads((app / "active.json").read_text(encoding="utf-8")))
        self.assertEqual(b"old", (old_version / "anicraft.exe").read_bytes())
        self.assertFalse((app / "versions" / SHA_B).exists())

    def test_fork_uses_python_sibling_with_argv(self):
        sibling = self.root / "storytold_forks.py"
        sibling.write_text("# fixture\n", encoding="utf-8")
        with mock.patch.object(install, "_run", return_value=completed()) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                install.command_fork(self.root, "anicraft")
        self.assertEqual(
            [install.sys.executable, sibling, "--root", self.root, "bootstrap", "anicraft"],
            run.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
