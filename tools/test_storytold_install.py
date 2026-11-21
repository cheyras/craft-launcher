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
            {"repo", "state", "remote_sha", "installed_sha", "installed_msi_version", "detail", "build_supported"},
            set(rows[0]),
        )
        self.assertTrue(rows[0]["build_supported"])
        self.assertTrue(rows[2]["build_supported"])
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

    def test_status_detects_legacy_msi_offline_without_inventing_source_sha(self):
        executable = self.local / "CadCraft" / "cadcraft.exe"
        executable.parent.mkdir()
        executable.write_bytes(b"legacy MSI")
        product = install.msi.InstalledProduct(
            "cadcraft", install.msi.UPGRADE_CODES["cadcraft"],
            "11111111-1111-1111-1111-111111111111", "1.2.3",
            str(executable), str(executable.parent),
        )
        with mock.patch.object(install, "_is_windows", return_value=True), mock.patch.object(
            install.msi, "inventory", return_value=product
        ), mock.patch.object(install.msi, "read_receipt", return_value=None), mock.patch.object(
            install, "query_forks", side_effect=install.InstallError("offline")
        ):
            row = install.status_rows(self.root, ["cadcraft"])[0]
        self.assertEqual("1.2.3", row["installed_msi_version"])
        self.assertIsNone(row["installed_sha"])
        self.assertIn("offline", row["detail"])

    def test_prepare_msi_cli_stages_without_apply(self):
        stdout = io.StringIO()
        with mock.patch.object(install, "prepare_msi_repo", return_value=SHA_B) as prepare, mock.patch.object(
            install, "_apply_prepared_msi"
        ) as apply, contextlib.redirect_stdout(stdout):
            code = install.main(["--root", str(self.root), "prepare-msi", "cadcraft"])
        self.assertEqual(0, code)
        prepare.assert_called_once_with(self.root.resolve(), "cadcraft")
        apply.assert_not_called()
        self.assertIn("prepared MSI", stdout.getvalue())

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
        with self.assertRaisesRegex(install.InstallError, "requires package and binary"):
            install.select_packages(metadata, "historically-unsupported")

        photo = {"packages": [
            {"name": "photocraft", "targets": [{"name": "photocraft", "kind": ["bin"]}]},
            {"name": "photocraft-cli", "targets": [{"name": "photocraft-cli", "kind": ["bin"]}]},
        ]}
        self.assertEqual(
            (["photocraft", "photocraft-cli"], ["photocraft", "photocraft-cli"]),
            install.select_packages(photo, "photocraft"),
        )

    def test_recipe_declarations_and_required_script(self):
        source = self.root / "recipe"
        windows = source / "packaging" / "windows"
        windows.mkdir(parents=True)
        script = windows / "package.ps1"
        script.write_text("$Bins = @('figcraft', 'figcraft-cli')\n", encoding="utf-8")
        self.assertEqual(["figcraft", "figcraft-cli"], install._require_windows_recipe(source, "figcraft"))
        script.write_text("$Bins = 'effectcraft', 'effectcraft-cli'\n", encoding="utf-8")
        self.assertEqual(
            ["effectcraft", "effectcraft-cli"], install._require_windows_recipe(source, "effectcraft")
        )
        script.unlink()
        with self.assertRaisesRegex(install.InstallError, "no documented Windows packaging recipe"):
            install._require_windows_recipe(source, "effectcraft")

    def test_special_recipe_feature_flags(self):
        target = "x86_64-pc-windows-msvc"
        photo = install._cargo_build_argv("photocraft", ["photocraft", "photocraft-cli"], target)
        light = install._cargo_build_argv("lightcraft", ["lightcraft", "lightcraft-cli"], target)
        ordinary = install._cargo_build_argv("designcraft", ["designcraft", "designcraft-cli"], target)
        self.assertEqual("heif", photo[photo.index("--features") + 1])
        self.assertEqual("lightcraft/heif,lightcraft-cli/heif", light[light.index("--features") + 1])
        self.assertNotIn("--features", ordinary)

    def test_install_bootstraps_incomplete_states_then_builds(self):
        for state in ("new", "needs-clone", "needs-branch"):
            with self.subTest(state=state), mock.patch.object(
                install, "status_rows", return_value=[{"state": state, "detail": state}]
            ), mock.patch.object(install, "_bootstrap") as bootstrap, mock.patch.object(
                install, "build_repo", return_value=SHA_B
            ) as build:
                self.assertEqual(SHA_B, install.install_repo(self.root, "anicraft"))
                bootstrap.assert_called_once_with(self.root, "anicraft")
                build.assert_called_once_with(self.root, "anicraft")

    def test_install_ready_builds_directly_and_hard_error_stops(self):
        with mock.patch.object(
            install, "status_rows", return_value=[{"state": "ready", "detail": "ready"}]
        ), mock.patch.object(install, "_bootstrap") as bootstrap, mock.patch.object(
            install, "build_repo", return_value=SHA_B
        ) as build:
            self.assertEqual(SHA_B, install.install_repo(self.root, "anicraft"))
            bootstrap.assert_not_called()
            build.assert_called_once()
        with mock.patch.object(
            install, "status_rows", return_value=[{"state": "error", "detail": "authentication failed"}]
        ), mock.patch.object(install, "_bootstrap") as bootstrap, mock.patch.object(
            install, "build_repo"
        ) as build:
            with self.assertRaisesRegex(install.InstallError, "status stage.*authentication failed"):
                install.install_repo(self.root, "anicraft")
            bootstrap.assert_not_called()
            build.assert_not_called()

    def test_install_cli_dispatches_and_reports_installed_sha(self):
        stdout = io.StringIO()
        with mock.patch.object(install, "install_repo", return_value=SHA_B) as command, contextlib.redirect_stdout(
            stdout
        ):
            code = install.main(["--root", str(self.root), "install", "anicraft"])
        self.assertEqual(0, code)
        command.assert_called_once_with(self.root.resolve(), "anicraft")
        self.assertEqual(f"installed anicraft at {SHA_B}\n", stdout.getvalue())

    def test_install_retry_resumes_after_bootstrap_and_build_failures(self):
        rows = [
            [{"state": "new", "detail": "new"}],
            [{"state": "needs-clone", "detail": "saved fork"}],
        ]
        with mock.patch.object(install, "status_rows", side_effect=rows), mock.patch.object(
            install, "_bootstrap", side_effect=[install.InstallError("fork failed"), None]
        ) as bootstrap, mock.patch.object(install, "build_repo", return_value=SHA_B) as build:
            with self.assertRaisesRegex(install.InstallError, "bootstrap stage.*fork failed"):
                install.install_repo(self.root, "anicraft")
            self.assertEqual(SHA_B, install.install_repo(self.root, "anicraft"))
            self.assertEqual(2, bootstrap.call_count)
            build.assert_called_once()

        rows = [
            [{"state": "needs-branch", "detail": "branch"}],
            [{"state": "ready", "detail": "saved clone"}],
        ]
        with mock.patch.object(install, "status_rows", side_effect=rows), mock.patch.object(
            install, "_bootstrap"
        ) as bootstrap, mock.patch.object(
            install, "build_repo", side_effect=[install.InstallError("compiler failed"), SHA_B]
        ) as build:
            with self.assertRaisesRegex(install.InstallError, "build stage.*compiler failed"):
                install.install_repo(self.root, "anicraft")
            self.assertEqual(SHA_B, install.install_repo(self.root, "anicraft"))
            bootstrap.assert_called_once()
            self.assertEqual(2, build.call_count)

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
        ), mock.patch.object(install, "_require_windows_recipe", return_value=None), mock.patch.object(
            install, "_stream_command", return_value=0
        ):
            installed = install.build_repo(self.root, "anicraft")
        self.assertEqual(SHA_B, installed)
        receipt = json.loads((self.local / "StorytoldForks" / "anicraft" / "active.json").read_text(encoding="utf-8"))
        self.assertEqual(SHA_B, receipt["sha"])
        self.assertTrue(
            (self.local / "StorytoldForks" / "anicraft" / "versions" / SHA_B / "anicraft.exe").is_file()
        )

    def test_build_same_sha_valid_receipt_skips_fetch_and_compile(self):
        clone = self.root / "anicraft"
        clone.mkdir()
        version = self.local / "StorytoldForks" / "anicraft" / "versions" / SHA_B
        version.mkdir(parents=True)
        executable = version / "anicraft.exe"
        executable.write_bytes(b"verified active bytes")
        receipt = {
            "schema": 1,
            "repo": "anicraft",
            "sha": SHA_B,
            "executable": "anicraft.exe",
            "files": ["anicraft.exe"],
            "sha256": {"anicraft.exe": install.hashlib.sha256(executable.read_bytes()).hexdigest()},
        }
        (version.parents[1] / "active.json").write_text(json.dumps(receipt), encoding="utf-8")
        with mock.patch.object(install, "_is_windows", return_value=True), mock.patch.object(
            install, "validate_clone", return_value=clone
        ), mock.patch.object(install, "_verified_remote_sha", return_value=SHA_B) as remote, mock.patch.object(
            install, "_run", side_effect=AssertionError("fast path fetched source")
        ), mock.patch.object(
            install, "_stream_command", side_effect=AssertionError("fast path compiled source")
        ):
            self.assertEqual(SHA_B, install.build_repo(self.root, "anicraft"))
        remote.assert_called_once_with("anicraft")

    def test_build_prioritizes_existing_msi_and_never_activates_portable_on_failure(self):
        executable = self.local / "CadCraft" / "cadcraft.exe"
        executable.parent.mkdir()
        executable.write_bytes(b"installed")
        product = install.msi.InstalledProduct(
            "cadcraft", install.msi.UPGRADE_CODES["cadcraft"],
            "11111111-1111-1111-1111-111111111111", "1.0.0",
            str(executable), str(executable.parent),
        )
        with mock.patch.object(install, "_is_windows", return_value=True), mock.patch.object(
            install, "_installed_msi", return_value=product
        ), mock.patch.object(install, "prepare_msi_repo", return_value=SHA_B) as prepare, mock.patch.object(
            install.msi, "read_receipt", return_value=None
        ), mock.patch.object(
            install, "_apply_prepared_msi", side_effect=install.InstallError("UAC cancelled")
        ), mock.patch.object(install, "_atomic_json") as portable_activate:
            with self.assertRaisesRegex(install.InstallError, "UAC cancelled"):
                install.build_repo(self.root, "cadcraft")
        prepare.assert_called_once_with(self.root, "cadcraft")
        portable_activate.assert_not_called()

    def test_pdfcraft_stages_verified_models_and_not_partial_files(self):
        source = self.root / "pdfcraft-source"
        source.mkdir()
        stage = self.root / "stage"
        stage.mkdir()
        model_bytes = b"model data"
        licence_bytes = b"licence text"
        (source / "ATTRIBUTION.toml").write_text(
            "\n".join([
                "[[fetched]]",
                'file = "ocr.rten"',
                'kind = "model"',
                f'sha256 = "{install.hashlib.sha256(model_bytes).hexdigest()}"',
                f'licence_sha256 = "{install.hashlib.sha256(licence_bytes).hexdigest()}"',
            ]),
            encoding="utf-8",
        )

        def fake_models(argv, **kwargs):
            models = Path(argv[-1])
            (models / "ocr.rten").write_bytes(model_bytes)
            (models / "ocr.rten.LICENCE.txt").write_bytes(licence_bytes)
            (models / "ATTRIBUTION.txt").write_text("OCR attribution\n", encoding="utf-8")
            (models / "abandoned.part").write_bytes(b"partial")
            return 0

        with mock.patch.object(install, "_stream_command", side_effect=fake_models) as stream:
            copied = install._stage_pdfcraft_models(source, stage, {"CARGO_TARGET_DIR": "dedicated"}, io.StringIO())
        self.assertEqual(
            ["models/ATTRIBUTION.txt", "models/ocr.rten", "models/ocr.rten.LICENCE.txt"], copied
        )
        self.assertFalse((stage / "models" / "abandoned.part").exists())
        self.assertEqual(["cargo", "xtask", "models", os.fspath(stage / "models")], stream.call_args.args[0])
        self.assertEqual(source, stream.call_args.kwargs["cwd"])
        self.assertEqual("dedicated", stream.call_args.kwargs["env"]["CARGO_TARGET_DIR"])

    def test_pdfcraft_model_failure_is_logged(self):
        source = self.root / "source"
        source.mkdir()
        model_digest = install.hashlib.sha256(b"model").hexdigest()
        licence_digest = install.hashlib.sha256(b"licence").hexdigest()
        (source / "ATTRIBUTION.toml").write_text(
            "\n".join([
                "[[fetched]]",
                'file = "ocr.rten"',
                'kind = "model"',
                f'sha256 = "{model_digest}"',
                f'licence_sha256 = "{licence_digest}"',
            ]),
            encoding="utf-8",
        )
        stage = self.root / "failed-stage"
        stage.mkdir()
        log = io.StringIO()
        with mock.patch.object(install, "_stream_command", return_value=7):
            with self.assertRaisesRegex(install.InstallError, "xtask models failed"):
                install._stage_pdfcraft_models(source, stage, {}, log)
        self.assertIn("cargo xtask models", log.getvalue())

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
            install, "_require_windows_recipe", return_value=None
        ), mock.patch.object(install, "_stream_command", return_value=1
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

    def test_open_forwards_exact_args_after_separator(self):
        executable = self.local / "StorytoldForks" / "anicraft" / "versions" / SHA_A / "anicraft.exe"
        receipt = {"_exe": str(executable)}
        with mock.patch.object(install, "_is_windows", return_value=True), mock.patch.object(
            install, "read_receipt", return_value=receipt
        ), mock.patch.object(install.subprocess, "Popen") as popen, contextlib.redirect_stdout(io.StringIO()):
            code = install.main([
                "--root", str(self.root), "open", "anicraft", "--", "--profile", "name with spaces", "-x-y"
            ])
        self.assertEqual(0, code)
        self.assertEqual(
            [str(executable), "--profile", "name with spaces", "-x-y"], popen.call_args.args[0]
        )
        self.assertEqual(executable.parent, popen.call_args.kwargs["cwd"])
        self.assertTrue(popen.call_args.kwargs["close_fds"])

    def test_open_rejects_ambiguous_args_without_separator(self):
        with mock.patch.object(install, "command_open") as command:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = install.main(["--root", str(self.root), "open", "anicraft", "--profile"])
        self.assertEqual(2, code)
        self.assertIn("require '--' separator", stderr.getvalue())
        command.assert_not_called()

    def test_open_rejects_args_before_separator(self):
        with mock.patch.object(install, "command_open") as command:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = install.main(["--root", str(self.root), "open", "anicraft", "before", "--", "after"])
        self.assertEqual(2, code)
        self.assertIn("must follow '--' separator", stderr.getvalue())
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
