"""Fail-closed Windows Installer support for the known Storytold products.

The module is importable on non-Windows hosts.  Native APIs are constructed
only by :func:`native_api`, which keeps source inspection and unit tests from
touching the host's installer inventory.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


UPGRADE_CODES = {
    "cadcraft": "119D7D76-0167-4A9E-8F37-09DA0235F68E",
    "deckcraft": "6F9C69E7-7825-40D2-BF26-C274DA676425",
    "designcraft": "49E87D30-BB68-4896-AA1D-F45C833CFC3C",
    "effectcraft": "AACA8593-C6ED-40A8-BA6C-CB325D8529F9",
    "filmcraft": "31E6FB14-9DBC-41DD-B24B-26B71FB31B62",
    "gridcraft": "5511327B-F305-41BE-92D0-EB84944E7C4D",
    "lightcraft": "229BB552-5EFD-479A-BA86-8CB6D926390C",
    "pdfcraft": "EA00C367-7270-4C50-9D8B-A7A5C88AD27F",
    "photocraft": "D9660F32-2D0A-4ABE-B73D-17BE25F1542C",
    "soundcraft": "EE53D3E5-D0AE-40F9-808D-9417FDEBF61A",
    "vectorcraft": "601C6437-08AE-4645-ADCC-27F036C2EA2F",
    "wordcraft": "075497F9-E861-45D0-9C7F-A74F8C34E6BA",
}
MANUFACTURER = "Learning Machines LLC"
GUID_RE = re.compile(r"^\{?([0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})\}?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SIGNING_NAMES = {"WINDOWS_CERTIFICATE", "WINDOWS_CERTIFICATE_PASSWORD", "GITHUB_ACTIONS", "SIGNTOOL"}
SIGNING_PREFIXES = ("AZURE_SIGNING_", "AZURE_CERT_PROFILE")


class MsiError(RuntimeError):
    pass


def _guid(value: str) -> str:
    match = GUID_RE.fullmatch(value.strip())
    if not match:
        raise MsiError(f"invalid MSI GUID: {value!r}")
    return match.group(1).upper()


def _digest(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            value = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise MsiError(f"could not hash {path}: {exc}") from exc
    return value


def numeric_version(value: str) -> tuple[int, ...]:
    parts = value.split(".")
    if not 1 <= len(parts) <= 4 or any(not item.isdecimal() for item in parts):
        raise MsiError(f"invalid MSI numeric version: {value!r}")
    result = tuple(int(item) for item in parts)
    if any(item > 65535 for item in result):
        raise MsiError(f"MSI version field is out of range: {value!r}")
    return result + (0,) * (4 - len(result))


def safe_install_dir(value: Path) -> Path:
    text = os.fspath(value)
    if not value.is_absolute() or any(ord(ch) < 32 for ch in text) or '"' in text:
        raise MsiError(f"unsafe registered installation folder: {text!r}")
    if value.name in {"", ".", ".."}:
        raise MsiError(f"unsafe registered installation folder: {text!r}")
    return value


@dataclass(frozen=True)
class InstalledProduct:
    repo: str
    upgrade_code: str
    product_code: str
    version: str
    executable: str
    install_dir: str
    scope: str = "machine"
    architecture: str = "x64"
    manufacturer: str = MANUFACTURER


@dataclass(frozen=True)
class PackageInfo:
    product_code: str
    upgrade_code: str
    product_name: str
    version: str
    manufacturer: str
    architecture: str
    allusers: str
    files: tuple[str, ...]
    upgrade_rows: tuple[Mapping[str, Any], ...]
    sequences: Mapping[str, int]


class WindowsMsiApi:
    """Small ctypes boundary around read-only Windows Installer operations."""

    ERROR_SUCCESS = 0
    ERROR_MORE_DATA = 234
    ERROR_NO_MORE_ITEMS = 259
    INSTALLSTATE_DEFAULT = 5

    def __init__(self) -> None:
        if os.name != "nt":
            raise MsiError("Windows Installer inventory is available only on Windows")
        self.msi = ctypes.WinDLL("msi", use_last_error=True)
        uint_pointer = ctypes.POINTER(ctypes.c_uint)
        self.msi.MsiGetProductInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, uint_pointer]
        self.msi.MsiGetProductInfoW.restype = ctypes.c_uint
        self.msi.MsiEnumRelatedProductsW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_wchar_p]
        self.msi.MsiEnumRelatedProductsW.restype = ctypes.c_uint
        self.msi.MsiQueryProductStateW.argtypes = [ctypes.c_wchar_p]
        self.msi.MsiQueryProductStateW.restype = ctypes.c_int
        self.msi.MsiOpenDatabaseW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, uint_pointer]
        self.msi.MsiOpenDatabaseW.restype = ctypes.c_uint
        self.msi.MsiDatabaseOpenViewW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, uint_pointer]
        self.msi.MsiDatabaseOpenViewW.restype = ctypes.c_uint
        self.msi.MsiViewExecute.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.msi.MsiViewExecute.restype = ctypes.c_uint
        self.msi.MsiViewFetch.argtypes = [ctypes.c_uint, uint_pointer]
        self.msi.MsiViewFetch.restype = ctypes.c_uint
        self.msi.MsiRecordGetFieldCount.argtypes = [ctypes.c_uint]
        self.msi.MsiRecordGetFieldCount.restype = ctypes.c_uint
        self.msi.MsiRecordGetStringW.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_wchar_p, uint_pointer]
        self.msi.MsiRecordGetStringW.restype = ctypes.c_uint
        self.msi.MsiGetSummaryInformationW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint, uint_pointer]
        self.msi.MsiGetSummaryInformationW.restype = ctypes.c_uint
        self.msi.MsiSummaryInfoGetPropertyW.argtypes = [
            ctypes.c_uint, ctypes.c_uint, uint_pointer, ctypes.POINTER(ctypes.c_int),
            ctypes.c_void_p, ctypes.c_wchar_p, uint_pointer,
        ]
        self.msi.MsiSummaryInfoGetPropertyW.restype = ctypes.c_uint
        self.msi.MsiCloseHandle.argtypes = [ctypes.c_uint]
        self.msi.MsiCloseHandle.restype = ctypes.c_uint

    def _product_info(self, product_code: str, name: str) -> str:
        size = ctypes.c_uint(0)
        rc = self.msi.MsiGetProductInfoW(product_code, name, None, ctypes.byref(size))
        if rc not in (self.ERROR_SUCCESS, self.ERROR_MORE_DATA):
            raise MsiError(f"MsiGetProductInfo({name}) failed for {product_code}: {rc}")
        buffer = ctypes.create_unicode_buffer(size.value + 1)
        size.value += 1
        rc = self.msi.MsiGetProductInfoW(product_code, name, buffer, ctypes.byref(size))
        if rc != self.ERROR_SUCCESS:
            raise MsiError(f"MsiGetProductInfo({name}) failed for {product_code}: {rc}")
        return buffer.value

    def related_products(self, upgrade_code: str) -> list[str]:
        result: list[str] = []
        index = 0
        while True:
            buffer = ctypes.create_unicode_buffer(39)
            rc = self.msi.MsiEnumRelatedProductsW("{" + _guid(upgrade_code) + "}", 0, index, buffer)
            if rc == self.ERROR_NO_MORE_ITEMS:
                return result
            if rc != self.ERROR_SUCCESS:
                raise MsiError(f"MsiEnumRelatedProducts failed: {rc}")
            result.append(_guid(buffer.value))
            index += 1

    def product_property(self, product_code: str, name: str) -> str:
        return self._product_info("{" + _guid(product_code) + "}", name)

    def product_state(self, product_code: str) -> int:
        return int(self.msi.MsiQueryProductStateW("{" + _guid(product_code) + "}"))

    def app_path(self, executable_name: str) -> str | None:
        import winreg

        key_name = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"
        values: list[str] = []
        for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_name, 0, winreg.KEY_READ | flag) as key:
                    value, kind = winreg.QueryValueEx(key, None)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MsiError(f"could not read HKLM App Paths for {executable_name}: {exc}") from exc
            if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                values.append(os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value)
        unique = {os.path.normcase(os.path.abspath(item)) for item in values}
        if len(unique) > 1:
            raise MsiError(f"conflicting HKLM App Paths registrations for {executable_name}")
        return values[0] if values else None

    def is_process_running(self, executable: str) -> bool:
        """Compare native process image paths without invoking task-list tools."""
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel.Process32FirstW.restype = wintypes.BOOL
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel.Process32NextW.restype = wintypes.BOOL
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)
        if snapshot in (0, ctypes.c_void_p(-1).value):
            raise MsiError("could not inspect running processes before MSI update")
        target = os.path.normcase(os.path.abspath(executable))
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            more = bool(kernel.Process32FirstW(snapshot, ctypes.byref(entry)))
            while more:
                process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if process:
                    try:
                        size = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                            if os.path.normcase(os.path.abspath(buffer.value)) == target:
                                return True
                    finally:
                        kernel.CloseHandle(process)
                more = bool(kernel.Process32NextW(snapshot, ctypes.byref(entry)))
            return False
        finally:
            kernel.CloseHandle(snapshot)

    def inspect_package(self, path: Path) -> PackageInfo:
        database = ctypes.c_uint(0)
        rc = self.msi.MsiOpenDatabaseW(os.fspath(path), ctypes.c_void_p(0), ctypes.byref(database))
        if rc != self.ERROR_SUCCESS:
            raise MsiError(f"could not open MSI database {path}: {rc}")
        try:
            def rows(sql: str) -> list[tuple[str, ...]]:
                view = ctypes.c_uint(0)
                if self.msi.MsiDatabaseOpenViewW(database, sql, ctypes.byref(view)) != 0:
                    raise MsiError(f"MSI query failed: {sql}")
                try:
                    if self.msi.MsiViewExecute(view, 0) != 0:
                        raise MsiError(f"MSI query execution failed: {sql}")
                    output: list[tuple[str, ...]] = []
                    while True:
                        record = ctypes.c_uint(0)
                        result = self.msi.MsiViewFetch(view, ctypes.byref(record))
                        if result == self.ERROR_NO_MORE_ITEMS:
                            return output
                        if result != 0:
                            raise MsiError(f"MSI query fetch failed: {result}")
                        try:
                            count = int(self.msi.MsiRecordGetFieldCount(record))
                            fields: list[str] = []
                            for field in range(1, count + 1):
                                length = ctypes.c_uint(0)
                                self.msi.MsiRecordGetStringW(record, field, None, ctypes.byref(length))
                                buffer = ctypes.create_unicode_buffer(length.value + 1)
                                length.value += 1
                                self.msi.MsiRecordGetStringW(record, field, buffer, ctypes.byref(length))
                                fields.append(buffer.value)
                            output.append(tuple(fields))
                        finally:
                            self.msi.MsiCloseHandle(record)
                finally:
                    self.msi.MsiCloseHandle(view)

            properties = dict(rows("SELECT `Property`,`Value` FROM `Property`"))
            upgrades = tuple(
                {
                    "upgrade_code": row[0], "version_min": row[1], "version_max": row[2],
                    "attributes": int(row[4] or "0"), "remove": row[5], "action_property": row[6],
                }
                for row in rows("SELECT `UpgradeCode`,`VersionMin`,`VersionMax`,`Language`,`Attributes`,`Remove`,`ActionProperty` FROM `Upgrade`")
            )
            sequences = {row[0]: int(row[1]) for row in rows("SELECT `Action`,`Sequence` FROM `InstallExecuteSequence`")}
            files = tuple(row[0] for row in rows("SELECT `FileName` FROM `File`"))
            summary = ctypes.c_uint(0)
            template = ""
            if self.msi.MsiGetSummaryInformationW(database, None, 0, ctypes.byref(summary)) == 0:
                try:
                    kind = ctypes.c_uint(0)
                    integer = ctypes.c_int(0)
                    length = ctypes.c_uint(0)
                    self.msi.MsiSummaryInfoGetPropertyW(summary, 7, ctypes.byref(kind), ctypes.byref(integer), None, None, ctypes.byref(length))
                    buffer = ctypes.create_unicode_buffer(length.value + 1)
                    length.value += 1
                    if self.msi.MsiSummaryInfoGetPropertyW(summary, 7, ctypes.byref(kind), ctypes.byref(integer), None, buffer, ctypes.byref(length)) == 0:
                        template = buffer.value
                finally:
                    self.msi.MsiCloseHandle(summary)
            architecture = "x64" if any(x in template.lower() for x in ("x64", "intel64")) else "x86"
            return PackageInfo(
                _guid(properties.get("ProductCode", "")), _guid(properties.get("UpgradeCode", "")),
                properties.get("ProductName", ""), properties.get("ProductVersion", ""),
                properties.get("Manufacturer", ""), architecture,
                properties.get("ALLUSERS", ""), files, upgrades, sequences,
            )
        finally:
            self.msi.MsiCloseHandle(database)


def native_api() -> WindowsMsiApi:
    return WindowsMsiApi()


def inventory(repo: str, api: Any | None = None) -> InstalledProduct | None:
    code = UPGRADE_CODES.get(repo)
    if code is None:
        return None
    api = api or native_api()
    products = api.related_products(code)
    if not products:
        return None
    if len(products) != 1:
        raise MsiError(f"ambiguous MSI inventory for {repo}: {len(products)} related products are registered")
    product_code = products[0]
    if api.product_state(product_code) != api.INSTALLSTATE_DEFAULT:
        raise MsiError(f"{repo} has a conflicting advertised or non-local MSI registration")
    version = api.product_property(product_code, "VersionString")
    manufacturer = api.product_property(product_code, "Publisher")
    assignment = api.product_property(product_code, "AssignmentType")
    if manufacturer != MANUFACTURER or assignment != "1":
        raise MsiError(f"{repo} MSI has conflicting manufacturer or non-machine scope")
    executable_name = f"{repo}.exe"
    registered = api.app_path(executable_name)
    if not registered:
        raise MsiError(f"{repo} MSI has no HKLM App Paths registration for {executable_name}")
    executable = Path(registered)
    if not executable.is_absolute() or executable.name.lower() != executable_name:
        raise MsiError(f"{repo} has an invalid registered executable path: {registered!r}")
    if not executable.is_file():
        raise MsiError(f"{repo} registered executable is missing: {executable}")
    install_location = api.product_property(product_code, "InstallLocation").strip()
    install_dir = safe_install_dir(Path(install_location) if install_location else executable.parent)
    if os.path.normcase(os.path.abspath(executable.parent)) != os.path.normcase(os.path.abspath(install_dir)):
        raise MsiError(f"{repo} InstallLocation conflicts with HKLM App Paths")
    numeric_version(version)
    return InstalledProduct(repo, _guid(code), _guid(product_code), version, os.fspath(executable), os.fspath(install_dir))


def receipt_path(data_root: Path, repo: str) -> Path:
    return data_root / repo / "msi-active.json"


def read_receipt(data_root: Path, installed: InstalledProduct) -> dict[str, Any] | None:
    path = receipt_path(data_root, installed.repo)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MsiError(f"could not read MSI receipt {path}: {exc}") from exc
    except UnicodeError:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    required = {
        "schema": 1, "repo": installed.repo, "scope": "machine", "product_code": installed.product_code,
        "upgrade_code": installed.upgrade_code, "version": installed.version,
        "executable": installed.executable,
    }
    if (
        not isinstance(value, dict) or type(value.get("schema")) is not int
        or any(value.get(key) != expected for key, expected in required.items())
    ):
        return None
    sha = value.get("source_sha")
    digest = value.get("executable_sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        return None
    if _digest(Path(installed.executable)) != digest:
        raise MsiError(f"installed executable does not match MSI receipt: {installed.executable}")
    return value


def prepared_path(data_root: Path, repo: str) -> Path:
    return data_root / repo / "prepared-msi.json"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def scrub_signing_env(source: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value for key, value in source.items()
        if key.upper() not in SIGNING_NAMES and not any(key.upper().startswith(prefix) for prefix in SIGNING_PREFIXES)
    }


def make_upgrade_rollback_safe(wxs: Path, expected_upgrade_code: str) -> bool:
    """Guard identity/scope and narrowly add a safe MajorUpgrade schedule."""
    text = wxs.read_text(encoding="utf-8")
    package = re.search(r"<Package\b[^>]*>", text, re.IGNORECASE | re.DOTALL)
    if not package:
        raise MsiError(f"WiX source has no Package element: {wxs}")
    upgrade = re.search(r"\bUpgradeCode\s*=\s*['\"]([^'\"]+)", package.group(0), re.IGNORECASE)
    scope = re.search(r"\bScope\s*=\s*['\"]([^'\"]+)", package.group(0), re.IGNORECASE)
    if not upgrade or _guid(upgrade.group(1)) != _guid(expected_upgrade_code):
        raise MsiError(f"WiX UpgradeCode does not match pinned identity for {wxs}")
    if not scope or scope.group(1).lower() not in {"permachine", "peruserormachine"}:
        raise MsiError(f"WiX package has unsupported install scope in {wxs}")
    major = re.search(r"<MajorUpgrade\b[^>]*/?>", text, re.IGNORECASE | re.DOTALL)
    if not major or not re.search(r"AllowSameVersionUpgrades\s*=\s*['\"]yes['\"]", major.group(0), re.IGNORECASE):
        raise MsiError(f"WiX recipe does not allow same-version major upgrades: {wxs}")
    schedule_match = re.search(r"\bSchedule\s*=\s*['\"]([^'\"]+)['\"]", major.group(0), re.IGNORECASE)
    safe = {"afterinstallinitialize", "afterinstallexecute", "afterinstallexecuteagain"}
    if schedule_match:
        if schedule_match.group(1).lower() not in safe:
            replacement = major.group(0)[:schedule_match.start(1)] + "afterInstallInitialize" + major.group(0)[schedule_match.end(1):]
        else:
            return False
    else:
        insert_at = major.group(0).rfind("/>")
        if insert_at < 0:
            insert_at = major.group(0).rfind(">")
        replacement = major.group(0)[:insert_at].rstrip() + ' Schedule="afterInstallInitialize"' + major.group(0)[insert_at:]
    wxs.write_text(text[:major.start()] + replacement + text[major.end():], encoding="utf-8")
    return True


def validate_package(info: PackageInfo, installed: InstalledProduct, repo: str) -> None:
    if info.upgrade_code != installed.upgrade_code or info.product_code == installed.product_code:
        raise MsiError("MSI identity does not implement a new product in the pinned upgrade family")
    if info.manufacturer != MANUFACTURER or info.architecture != installed.architecture:
        raise MsiError("MSI manufacturer or architecture differs from the installed product")
    if re.sub(r"[^a-z0-9]", "", info.product_name.lower()) != repo.replace("-", ""):
        raise MsiError("MSI ProductName does not match the expected application identity")
    if info.allusers not in {"1", "2"} or installed.scope != "machine":
        raise MsiError("MSI would switch installation context")
    # Windows Installer compares only the first three ProductVersion fields.
    current = numeric_version(installed.version)[:3]
    candidate = numeric_version(info.version)[:3]
    if candidate < current:
        raise MsiError("MSI package would downgrade the installed product")
    removal_rows: list[tuple[Mapping[str, Any], int]] = []
    for item in info.upgrade_rows:
        try:
            same_family = _guid(str(item.get("upgrade_code", ""))) == installed.upgrade_code
            attributes = int(item.get("attributes", 0))
        except (MsiError, TypeError, ValueError):
            continue
        remove = str(item.get("remove") or "").upper()
        if not same_family or attributes & (0x2 | 0x4) or remove not in {"", "ALL"} or not item.get("action_property"):
            continue
        try:
            lower = numeric_version(str(item.get("version_min") or "0"))[:3]
            upper = numeric_version(str(item.get("version_max") or "65535.65535.65535"))[:3]
        except MsiError:
            continue
        if current < lower or current > upper:
            continue
        if current == lower and not (attributes & 0x100):
            continue
        if current == upper and not (attributes & 0x200):
            continue
        if candidate == current and not (attributes & 0x200):
            continue
        removal_rows.append((item, attributes))
    if len(removal_rows) != 1:
        raise MsiError(
            "MSI Upgrade table must contain exactly one permitted complete related-product removal row "
            "for the installed version; detect-only and ignore-removal-failure rows are not permitted"
        )
    sequence = info.sequences.get("RemoveExistingProducts")
    initialize = info.sequences.get("InstallInitialize")
    finalize = info.sequences.get("InstallFinalize")
    if sequence is None or initialize is None or finalize is None or not initialize < sequence < finalize:
        raise MsiError("RemoveExistingProducts is outside the rollback-safe transaction window")
    expected = f"{repo}.exe"
    names = {item.split("|")[-1].lower() for item in info.files}
    if expected not in names:
        raise MsiError(f"MSI payload does not contain {expected}")


def validate_prepared(data_root: Path, installed: InstalledProduct, source_sha: str, api: Any | None = None) -> tuple[Path, PackageInfo, dict[str, Any]]:
    path = prepared_path(data_root, installed.repo)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MsiError(f"no usable prepared MSI for {installed.repo}: {exc}") from exc
    if not isinstance(value, dict):
        raise MsiError("prepared MSI manifest is not an object")
    if type(value.get("schema")) is not int or value.get("schema") != 1 or value.get("repo") != installed.repo:
        raise MsiError("prepared MSI manifest has the wrong schema or application")
    if value.get("source_sha") != source_sha or value.get("installed") != asdict(installed):
        raise MsiError("prepared MSI source or installation context has changed; run prepare-msi again")
    package_value = value.get("package")
    digest = value.get("package_sha256")
    executable_digest = value.get("executable_sha256")
    if (
        not isinstance(package_value, str) or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
        or not isinstance(executable_digest, str) or not SHA256_RE.fullmatch(executable_digest)
    ):
        raise MsiError("prepared MSI manifest has invalid package provenance")
    package = Path(package_value)
    if not package.is_absolute() or not package.is_file() or _digest(package) != digest:
        raise MsiError("prepared MSI package is missing or has changed")
    info = (api or native_api()).inspect_package(package)
    validate_package(info, installed, installed.repo)
    return package, info, value


def install_prepared(data_root: Path, installed: InstalledProduct, source_sha: str, api: Any | None = None) -> str:
    api = api or native_api()
    package, info, prepared = validate_prepared(data_root, installed, source_sha, api)
    # Inventory again immediately before the elevated mutation.
    current = inventory(installed.repo, api)
    if current != installed:
        raise MsiError("installation changed after preparation; no MSI was run")
    process_check = getattr(api, "is_process_running", None)
    if process_check is not None and process_check(installed.executable):
        raise MsiError(f"close {Path(installed.executable).name} before installing the update")
    log = data_root / installed.repo / "logs" / f"msi-{source_sha}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    msiexec = system_root / "System32" / "msiexec.exe"
    argv = [os.fspath(msiexec), "/i", os.fspath(package), "/passive", "/norestart", "/l*v", os.fspath(log), "ALLUSERS=1"]
    install_dir = os.fspath(safe_install_dir(Path(installed.install_dir)))
    command = subprocess.list2cmdline(argv) + f' INSTALLFOLDER="{install_dir}"'
    result = subprocess.run(command, executable=os.fspath(msiexec), shell=False, check=False)
    if result.returncode not in (0, 3010):
        detail = " (installation was cancelled)" if result.returncode in (1223, 1602) else ""
        raise MsiError(f"Windows Installer failed with {result.returncode}{detail}; log: {log}")
    new = inventory(installed.repo, api)
    if new is None or new.product_code != info.product_code or new.product_code == installed.product_code or new.version != info.version:
        raise MsiError(f"MSI postcheck did not find exactly the new related product; log: {log}")
    related = {_guid(item) for item in api.related_products(installed.upgrade_code)}
    if related != {new.product_code} or installed.product_code in related:
        raise MsiError(f"old MSI registration remains or related-product inventory is ambiguous; log: {log}")
    if os.path.normcase(new.executable) != os.path.normcase(installed.executable):
        raise MsiError(f"MSI changed the registered executable path; log: {log}")
    expected_exe_hash = prepared.get("executable_sha256")
    if not isinstance(expected_exe_hash, str) or _digest(Path(new.executable)) != expected_exe_hash:
        raise MsiError(f"installed executable does not equal the verified packaged payload; log: {log}")
    receipt = {
        "schema": 1, "repo": new.repo, "scope": new.scope, "product_code": new.product_code,
        "upgrade_code": new.upgrade_code, "version": new.version, "executable": new.executable,
        "executable_sha256": expected_exe_hash, "source_sha": source_sha,
        "restart_required": result.returncode == 3010,
    }
    _atomic_json(receipt_path(data_root, new.repo), receipt)
    if result.returncode == 3010:
        print(f"warning: {new.repo} was updated successfully; Windows reports that a restart is required", file=sys.stderr)
    return source_sha
