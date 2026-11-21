import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import storytold_msi as msi


OLD = "11111111-1111-1111-1111-111111111111"
NEW = "22222222-2222-2222-2222-222222222222"
SHA = "a" * 40


class FakeApi:
    INSTALLSTATE_DEFAULT = 5

    def __init__(self, executable: Path):
        self.executable = executable
        self.products = [OLD]
        self.package = None

    def related_products(self, _upgrade):
        return list(self.products)

    def product_state(self, _product):
        return self.INSTALLSTATE_DEFAULT

    def product_property(self, product, name):
        values = {
            "VersionString": "2.0.0" if product == NEW else "1.0.0",
            "Publisher": msi.MANUFACTURER,
            "AssignmentType": "1",
            "InstallLocation": str(self.executable.parent),
        }
        return values[name]

    def app_path(self, _name):
        return str(self.executable)

    def inspect_package(self, _path):
        return self.package

    def is_process_running(self, _path):
        return False


def package_info(repo="cadcraft", sequence=1600, attributes=0x300):
    return msi.PackageInfo(
        product_code=NEW,
        upgrade_code=msi.UPGRADE_CODES[repo],
        product_name="CadCraft",
        version="2.0.0",
        manufacturer=msi.MANUFACTURER,
        architecture="x64",
        allusers="2",
        files=(f"{repo}.exe",),
        upgrade_rows=({
            "upgrade_code": msi.UPGRADE_CODES[repo],
            "version_min": "0.0.0",
            "version_max": "2.0.0",
            "attributes": attributes,
            "remove": "",
            "action_property": "WIX_UPGRADE_DETECTED",
        },),
        sequences={"InstallInitialize": 1500, "RemoveExistingProducts": sequence, "InstallFinalize": 6600},
    )


class MsiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "Cad Craft" / "cadcraft.exe"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"verified exe")
        self.api = FakeApi(self.exe)

    def test_inventory_uses_upgrade_family_and_registered_absolute_executable(self):
        product = msi.inventory("cadcraft", self.api)
        self.assertEqual(OLD, product.product_code)
        self.assertEqual("1.0.0", product.version)
        self.api.products.append(NEW)
        with self.assertRaisesRegex(msi.MsiError, "ambiguous"):
            msi.inventory("cadcraft", self.api)

    def test_inventory_uses_installed_publisher_property(self):
        with mock.patch.object(self.api, "product_property", wraps=self.api.product_property) as prop:
            msi.inventory("cadcraft", self.api)
        self.assertIn(mock.call(OLD, "Publisher"), prop.call_args_list)
        self.assertNotIn(mock.call(OLD, "Manufacturer"), prop.call_args_list)

    def test_recipe_edit_is_guarded_and_rollback_safe(self):
        wxs = self.root / "cadcraft.wxs"
        wxs.write_text(
            f'<Wix><Package UpgradeCode="{msi.UPGRADE_CODES["cadcraft"]}" Scope="perUserOrMachine">'
            '<MajorUpgrade AllowSameVersionUpgrades="yes" /></Package></Wix>',
            encoding="utf-8",
        )
        self.assertTrue(msi.make_upgrade_rollback_safe(wxs, msi.UPGRADE_CODES["cadcraft"]))
        self.assertIn('Schedule="afterInstallInitialize"', wxs.read_text(encoding="utf-8"))
        self.assertFalse(msi.make_upgrade_rollback_safe(wxs, msi.UPGRADE_CODES["cadcraft"]))
        with self.assertRaisesRegex(msi.MsiError, "pinned identity"):
            msi.make_upgrade_rollback_safe(wxs, msi.UPGRADE_CODES["deckcraft"])

    def test_preflight_rejects_unsafe_upgrade_and_accepts_transactional_one(self):
        installed = msi.inventory("cadcraft", self.api)
        msi.validate_package(package_info(), installed, "cadcraft")
        for info, message in (
            (package_info(sequence=1400), "rollback-safe"),
            (package_info(attributes=0x302), "detect-only"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(msi.MsiError, message):
                msi.validate_package(info, installed, "cadcraft")
        partial = package_info()
        partial.upgrade_rows[0]["remove"] = "MainFeature"
        with self.assertRaisesRegex(msi.MsiError, "complete related-product removal"):
            msi.validate_package(partial, installed, "cadcraft")
        wrong = package_info()
        wrong = msi.PackageInfo(**{**wrong.__dict__, "architecture": "x86"})
        with self.assertRaisesRegex(msi.MsiError, "architecture"):
            msi.validate_package(wrong, installed, "cadcraft")

    def test_preflight_selects_real_removal_row_independent_of_order(self):
        installed = msi.inventory("cadcraft", self.api)
        real = {
            "upgrade_code": installed.upgrade_code, "version_min": "", "version_max": "1.0.0",
            "attributes": 513, "remove": "ALL", "action_property": "WIX_UPGRADE_DETECTED",
        }
        detect = {
            "upgrade_code": installed.upgrade_code, "version_min": "1.0.0", "version_max": "",
            "attributes": 2, "remove": "", "action_property": "WIX_DOWNGRADE_DETECTED",
        }
        base = package_info()
        for rows in ((real, detect), (detect, real)):
            with self.subTest(rows=rows):
                info = msi.PackageInfo(**{**base.__dict__, "upgrade_rows": rows})
                msi.validate_package(info, installed, "cadcraft")

    def test_preflight_requires_one_safe_inclusive_removal_row(self):
        installed = msi.inventory("cadcraft", self.api)
        base = package_info()
        unsafe = []
        for attributes, remove in ((2, ""), (4, ""), (0x300, "MainFeature"), (0x100, "")):
            row = dict(base.upgrade_rows[0], attributes=attributes, remove=remove, version_max="1.0.0")
            unsafe.append(msi.PackageInfo(**{**base.__dict__, "upgrade_rows": (row,)}))
        for info in unsafe:
            with self.subTest(row=info.upgrade_rows[0]), self.assertRaisesRegex(msi.MsiError, "exactly one permitted"):
                msi.validate_package(info, installed, "cadcraft")
        duplicate = msi.PackageInfo(**{**base.__dict__, "upgrade_rows": (base.upgrade_rows[0], base.upgrade_rows[0])})
        with self.assertRaisesRegex(msi.MsiError, "exactly one permitted"):
            msi.validate_package(duplicate, installed, "cadcraft")

    def test_preflight_compares_only_three_msi_version_fields(self):
        installed = msi.InstalledProduct(**{**asdict(msi.inventory("cadcraft", self.api)), "version": "1.0.0.9"})
        info = package_info(attributes=0x300)
        info.upgrade_rows[0]["version_max"] = "1.0.0"
        msi.validate_package(info, installed, "cadcraft")

    def test_prepared_install_revalidates_then_writes_one_receipt_after_unique_postcheck(self):
        installed = msi.inventory("cadcraft", self.api)
        self.api.package = package_info()
        package = self.root / "prepared.msi"
        package.write_bytes(b"fixture MSI")
        prepared = {
            "schema": 1,
            "repo": "cadcraft",
            "source_sha": SHA,
            "installed": asdict(installed),
            "package": str(package),
            "package_sha256": msi._digest(package),
            "executable_sha256": msi._digest(self.exe),
        }
        msi._atomic_json(msi.prepared_path(self.root, "cadcraft"), prepared)
        msi._atomic_json(msi.receipt_path(self.root, "cadcraft"), {
            "schema": 1, "repo": "cadcraft", "scope": "machine", "product_code": NEW,
            "upgrade_code": installed.upgrade_code, "version": "0.9.0", "executable": installed.executable,
            "source_sha": "b" * 40, "executable_sha256": msi._digest(self.exe),
        })
        self.assertIsNone(msi.read_receipt(self.root, installed))

        def apply(_argv, **_kwargs):
            self.api.products = [NEW]
            return subprocess.CompletedProcess([], 0)

        with mock.patch.object(msi.subprocess, "run", side_effect=apply) as run:
            self.assertEqual(SHA, msi.install_prepared(self.root, installed, SHA, self.api))
        command = run.call_args.args[0]
        self.assertIsInstance(command, str)
        self.assertIn(" ALLUSERS=1 ", command)
        self.assertIn(f'INSTALLFOLDER="{installed.install_dir}"', command)
        self.assertNotIn(f'"INSTALLFOLDER={installed.install_dir}"', command)
        self.assertFalse(run.call_args.kwargs["shell"])
        receipt = json.loads(msi.receipt_path(self.root, "cadcraft").read_text(encoding="utf-8"))
        self.assertEqual(NEW, receipt["product_code"])
        self.assertEqual(SHA, receipt["source_sha"])
        self.assertFalse((self.root / "cadcraft" / "active.json").exists())

    def test_postcheck_does_not_accept_candidate_code_with_wrong_version(self):
        installed = msi.inventory("cadcraft", self.api)
        self.api.package = package_info()
        package = self.root / "prepared.msi"
        package.write_bytes(b"fixture MSI")
        msi._atomic_json(msi.prepared_path(self.root, "cadcraft"), {
            "schema": 1, "repo": "cadcraft", "source_sha": SHA, "installed": asdict(installed),
            "package": str(package), "package_sha256": msi._digest(package),
            "executable_sha256": msi._digest(self.exe),
        })

        original_property = self.api.product_property
        def restored_property(product, name):
            if product == NEW and name == "VersionString":
                return "1.5.0"
            return original_property(product, name)
        def apply(_command, **_kwargs):
            self.api.products = [NEW]
            return subprocess.CompletedProcess([], 0)

        with mock.patch.object(self.api, "product_property", side_effect=restored_property), mock.patch.object(
            msi.subprocess, "run", side_effect=apply
        ):
            with self.assertRaisesRegex(msi.MsiError, "postcheck"):
                msi.install_prepared(self.root, installed, SHA, self.api)
        self.assertFalse(msi.receipt_path(self.root, "cadcraft").exists())

    def test_changed_prepared_hash_fails_before_mutation(self):
        installed = msi.inventory("cadcraft", self.api)
        package = self.root / "prepared.msi"
        package.write_bytes(b"first")
        value = {
            "schema": 1, "repo": "cadcraft", "source_sha": SHA,
            "installed": asdict(installed), "package": str(package),
            "package_sha256": msi._digest(package), "executable_sha256": msi._digest(self.exe),
        }
        msi._atomic_json(msi.prepared_path(self.root, "cadcraft"), value)
        package.write_bytes(b"changed")
        with self.assertRaisesRegex(msi.MsiError, "has changed"), mock.patch.object(msi.subprocess, "run") as run:
            msi.install_prepared(self.root, installed, SHA, self.api)
        run.assert_not_called()

    def test_stale_or_malformed_receipt_is_unadopted_but_current_hash_mismatch_fails(self):
        installed = msi.inventory("cadcraft", self.api)
        receipt = msi.receipt_path(self.root, "cadcraft")
        receipt.parent.mkdir(parents=True)
        receipt.write_text("{bad json", encoding="utf-8")
        self.assertIsNone(msi.read_receipt(self.root, installed))
        stale = {
            "schema": 1, "repo": "cadcraft", "scope": "machine", "product_code": NEW,
            "upgrade_code": installed.upgrade_code, "version": "2.0.0", "executable": installed.executable,
            "source_sha": SHA, "executable_sha256": msi._digest(self.exe),
        }
        receipt.write_text(json.dumps(stale), encoding="utf-8")
        self.assertIsNone(msi.read_receipt(self.root, installed))
        current = {**stale, "product_code": installed.product_code, "version": installed.version,
                   "executable_sha256": "0" * 64}
        receipt.write_text(json.dumps(current), encoding="utf-8")
        with self.assertRaisesRegex(msi.MsiError, "does not match MSI receipt"):
            msi.read_receipt(self.root, installed)

    def test_prepared_manifest_requires_object_schema_and_application(self):
        installed = msi.inventory("cadcraft", self.api)
        path = msi.prepared_path(self.root, "cadcraft")
        path.parent.mkdir(parents=True)
        for value in ([], {"schema": True, "repo": "cadcraft"}, {"schema": 1, "repo": "deckcraft"}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(msi.MsiError, "object|schema or application"):
                    msi.validate_prepared(self.root, installed, SHA, self.api)


if __name__ == "__main__":
    unittest.main()
