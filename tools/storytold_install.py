#!/usr/bin/env python3
"""Install verified ``cheyras`` Storytold forks from their ``chey`` branch.

This module deliberately uses only the Python standard library.  All external
programs are invoked with argv lists (never through a shell), which also makes
the security-sensitive Git and Cargo boundaries straightforward to test.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Sequence

import storytold_msi as msi


ACCOUNT = "cheyras"
UPSTREAM = "storytold"
BRANCH = "chey"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
GITHUB_URL_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?$",
    re.IGNORECASE,
)
LAUNCHER_REPO = "craft-launcher"
FEATURES = {
    "photocraft": "heif",
    "lightcraft": "lightcraft/heif,lightcraft-cli/heif",
}
NOTICE_NAMES = ("README.md", "LICENSE", "LICENSE-MIT", "LICENSE-APACHE", "NOTICE", "ATTRIBUTION.md")


class InstallError(RuntimeError):
    """An expected, user-actionable installer failure."""


class MixedGitHubResponse(InstallError):
    """A partial response that must not be flattened into per-repo states."""


def validate_slug(value: str) -> str:
    slug = value.strip().lower()
    if value != slug or not SLUG_RE.fullmatch(slug) or "--" in slug:
        raise InstallError(f"invalid repository slug: {value!r}")
    return slug


def _run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [os.fspath(item) for item in argv],
            cwd=cwd,
            env=env,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _is_windows() else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"{argv[0]} timed out after {timeout:g} seconds") from exc
    except OSError as exc:
        raise InstallError(f"could not run {argv[0]}: {exc}") from exc
    if check and result.returncode:
        message = (result.stderr or result.stdout or "").strip()
        raise InstallError(f"{argv[0]} failed ({result.returncode}): {message}")
    return result


def _github_repo(url: str) -> tuple[str, str] | None:
    match = GITHUB_URL_RE.fullmatch(url.strip().rstrip("/"))
    return (match.group(1).lower(), match.group(2).lower()) if match else None


def _git_url(repo: Path, remote: str) -> str:
    result = _run(["git", "-C", repo, "remote", "get-url", remote], check=False)
    if result.returncode:
        raise InstallError(f"{repo} has no {remote!r} Git remote")
    return result.stdout.strip()


def validate_clone(root: Path, repo: str) -> Path:
    clone = root / repo
    result = _run(["git", "-C", clone, "rev-parse", "--is-inside-work-tree"], check=False)
    if result.returncode or result.stdout.strip() != "true":
        raise InstallError(f"missing or invalid local clone: {clone}")
    if _github_repo(_git_url(clone, "origin")) != (ACCOUNT, repo):
        raise InstallError(f"{clone} origin is not https://github.com/{ACCOUNT}/{repo}")
    if _github_repo(_git_url(clone, "upstream")) != (UPSTREAM, repo):
        raise InstallError(f"{clone} upstream is not https://github.com/{UPSTREAM}/{repo}")
    return clone


def data_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise InstallError("LOCALAPPDATA is not set; this command requires a Windows user profile")
    return Path(local) / "StorytoldForks"


def _receipt_path(repo: str) -> Path:
    return data_root() / repo / "active.json"


def read_receipt(repo: str) -> dict[str, Any] | None:
    # Status validation is intentionally usable in Linux test/scout
    # environments; only Windows build/open require the per-user data root.
    if not os.environ.get("LOCALAPPDATA") and os.name != "nt":
        return None
    path = _receipt_path(repo)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"invalid install receipt {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"invalid install receipt {path}: expected an object")
    version = data_root() / repo / "versions" / str(value.get("sha", ""))
    return _validate_receipt_payload(repo, value, version, path)


def _validate_receipt_payload(
    repo: str, value: dict[str, Any], version: Path, receipt_path: Path
) -> dict[str, Any]:
    """Validate a receipt and every byte of its immutable payload."""
    if value.get("schema") != 1 or value.get("repo") != repo:
        raise InstallError(f"invalid install receipt {receipt_path}: wrong schema or repository")
    sha = value.get("sha")
    executable = value.get("executable")
    files = value.get("files")
    hashes = value.get("sha256")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise InstallError(f"invalid install receipt {receipt_path}: bad commit SHA")
    if not isinstance(executable, str) or not executable or Path(executable).is_absolute():
        raise InstallError(f"invalid install receipt {receipt_path}: bad executable path")
    if not isinstance(files, list) or not files or any(not isinstance(x, str) for x in files):
        raise InstallError(f"invalid install receipt {receipt_path}: bad file list")
    if len(set(files)) != len(files) or not isinstance(hashes, dict) or set(hashes) != set(files):
        raise InstallError(f"invalid install receipt {receipt_path}: bad file hashes")
    if executable not in files:
        raise InstallError(f"invalid install receipt {receipt_path}: executable is not in file list")
    version = version.resolve()
    exe = (version / executable).resolve()
    try:
        exe.relative_to(version)
    except ValueError as exc:
        raise InstallError(f"invalid install receipt {receipt_path}: executable escapes version directory") from exc
    if not exe.is_file():
        raise InstallError(f"installed executable is missing: {exe}")
    for relative in files:
        candidate = (version / relative).resolve()
        try:
            candidate.relative_to(version)
        except ValueError as exc:
            raise InstallError(f"invalid install receipt {receipt_path}: file escapes version directory") from exc
        if not candidate.is_file():
            raise InstallError(f"installed file is missing: {candidate}")
        expected = hashes.get(relative)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise InstallError(f"invalid install receipt {receipt_path}: bad hash for {relative}")
        try:
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError as exc:
            raise InstallError(f"could not validate installed file {candidate}: {exc}") from exc
        if actual != expected:
            raise InstallError(f"installed file does not match its receipt: {candidate}")
    value["_exe"] = str(exe)
    return value


def _graphql_query(repos: Sequence[str]) -> tuple[str, dict[str, str]]:
    variables: list[str] = []
    fields: list[str] = []
    mapping: dict[str, str] = {}
    for index, repo in enumerate(repos):
        key = f"r{index}"
        variable = f"n{index}"
        mapping[key] = repo
        variables.append(f"${variable}:String!")
        fields.append(
            f'{key}:repository(owner:"{ACCOUNT}",name:${variable}){{'
            "nameWithOwner isFork parent{name owner{login}} "
            f'ref(qualifiedName:"refs/heads/{BRANCH}"){{target{{... on Commit{{oid}}}}}}'
            "}"
        )
    return f"query({','.join(variables)}){{{''.join(fields)}}}", mapping


def query_forks(repos: Sequence[str]) -> dict[str, dict[str, Any] | None]:
    """Return exact fork/branch metadata using one bounded GraphQL request."""
    query, mapping = _graphql_query(repos)
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for index, repo in enumerate(repos):
        argv.extend(["-F", f"n{index}={repo}"])
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    result = _run(argv, check=False, timeout=30.0, env=env)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        detail = (result.stderr or "GitHub status query returned invalid JSON").strip()
        raise InstallError(f"GitHub status query failed: {detail}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise InstallError("GitHub status query returned no data")
    errors = payload.get("errors", [])
    if not isinstance(errors, list):
        raise InstallError("GitHub status query returned an invalid errors list")
    missing_aliases: set[str] = set()
    unknown_errors: list[str] = []
    for error in errors:
        path = error.get("path") if isinstance(error, dict) else None
        alias = path[0] if isinstance(path, list) and len(path) == 1 else None
        message = str(error.get("message", "GitHub query error")) if isinstance(error, dict) else str(error)
        repo = mapping.get(alias) if isinstance(alias, str) else None
        corroborated = repo is not None and alias in payload["data"] and payload["data"][alias] is None
        names_repo = repo is not None and f"'{ACCOUNT}/{repo}'" in message
        if (
            isinstance(error, dict)
            and error.get("type") == "NOT_FOUND"
            and corroborated
            and names_repo
            and "Could not resolve to a Repository" in message
        ):
            missing_aliases.add(alias)
        else:
            unknown_errors.append(message)
    if unknown_errors or (result.returncode and not missing_aliases):
        detail = "; ".join(unknown_errors) or (result.stderr or f"gh exited {result.returncode}").strip()
        error = MixedGitHubResponse if missing_aliases and unknown_errors else InstallError
        raise error("GitHub status query error: " + detail)
    output: dict[str, dict[str, Any] | None] = {}
    for key, repo in mapping.items():
        node = payload["data"].get(key)
        if node is None:
            if key in missing_aliases:
                output[repo] = None
            else:
                raise InstallError(f"GitHub returned an unexplained empty result for {ACCOUNT}/{repo}")
        elif not isinstance(node, dict):
            raise InstallError(f"GitHub returned invalid metadata for {ACCOUNT}/{repo}")
        else:
            _validate_remote_node(repo, node, require_branch=False)
            output[repo] = node
    return output


def _validate_remote_node(repo: str, node: dict[str, Any], *, require_branch: bool) -> str | None:
    """Validate the complete queried identity, optionally returning its branch SHA."""
    parent = node.get("parent")
    owner = parent.get("owner") if isinstance(parent, dict) else None
    if (
        node.get("nameWithOwner") != f"{ACCOUNT}/{repo}"
        or node.get("isFork") is not True
        or not isinstance(parent, dict)
        or parent.get("name") != repo
        or not isinstance(owner, dict)
        or owner.get("login") != UPSTREAM
    ):
        raise InstallError(f"{ACCOUNT}/{repo} is not a fork of {UPSTREAM}/{repo}")
    ref = node.get("ref")
    oid = ref.get("target", {}).get("oid") if isinstance(ref, dict) else None
    if oid is None and not require_branch:
        return None
    if not isinstance(oid, str) or not SHA_RE.fullmatch(oid):
        raise InstallError(f"{ACCOUNT}/{repo} has no valid refs/heads/{BRANCH}")
    return oid


def _row(
    repo: str,
    state: str,
    detail: str,
    remote: str | None = None,
    installed: str | None = None,
    installed_msi_version: str | None = None,
) -> dict[str, Any]:
    return {
        "repo": repo,
        "state": state,
        "remote_sha": remote,
        "installed_sha": installed,
        "installed_msi_version": installed_msi_version,
        "detail": detail,
        # This is capability discovery, not a frozen catalogue. A verified
        # Storytold repository is a candidate; build_repo validates its actual
        # source-local Windows recipe and Cargo metadata before compiling.
        "build_supported": state != "error",
    }


def status_rows(root: Path, repos: Sequence[str]) -> list[dict[str, Any]]:
    installed_msi: dict[str, tuple[msi.InstalledProduct, dict[str, Any] | None] | None] = {}
    msi_errors: dict[str, str] = {}
    if _is_windows():
        for repo in repos:
            if repo not in msi.UPGRADE_CODES:
                continue
            try:
                product = msi.inventory(repo)
                if product is not None:
                    if _receipt_path(repo).exists():
                        raise InstallError(
                            f"{repo} has both MSI registration and a portable active receipt; resolve the ambiguity"
                        )
                    installed_msi[repo] = (product, msi.read_receipt(data_root(), product))
                else:
                    installed_msi[repo] = None
            except (msi.MsiError, InstallError) as exc:
                msi_errors[repo] = str(exc)
    try:
        metadata = query_forks(repos)
    except MixedGitHubResponse:
        # A confirmed absence mixed with an unrelated denial cannot safely be
        # represented as independent rows: reject the entire lookup.
        raise
    except InstallError as exc:
        # Status is a machine protocol: even service/auth failures are rows,
        # never prose or a misleading collection of "new" repositories.
        rows: list[dict[str, Any]] = []
        for repo in repos:
            if repo in msi_errors:
                rows.append(_row(repo, "error", msi_errors[repo]))
                continue
            try:
                msi_value = installed_msi.get(repo)
                if msi_value:
                    product, receipt = msi_value
                    installed = receipt["source_sha"] if receipt else None
                    version = product.version
                else:
                    receipt = read_receipt(repo)
                    installed = receipt["sha"] if receipt else None
                    version = None
            except InstallError as receipt_error:
                rows.append(_row(repo, "error", f"{exc}; {receipt_error}"))
            else:
                rows.append(_row(repo, "error", str(exc), installed=installed, installed_msi_version=version))
        return rows
    rows: list[dict[str, Any]] = []
    for repo in repos:
        if repo in msi_errors:
            rows.append(_row(repo, "error", msi_errors[repo]))
            continue
        node = metadata[repo]
        if node is None:
            value = installed_msi.get(repo)
            version = value[0].version if value else None
            installed = value[1]["source_sha"] if value and value[1] else None
            rows.append(_row(repo, "new", f"{ACCOUNT}/{repo} does not exist", installed=installed, installed_msi_version=version))
            continue
        try:
            oid = _validate_remote_node(repo, node, require_branch=False)
        except InstallError as exc:
            rows.append(_row(repo, "error", str(exc)))
            continue
        if oid is None:
            value = installed_msi.get(repo)
            version = value[0].version if value else None
            installed = value[1]["source_sha"] if value and value[1] else None
            rows.append(_row(repo, "needs-branch", f"fork exists but refs/heads/{BRANCH} is missing", installed=installed, installed_msi_version=version))
            continue
        clone = root / repo
        if not clone.exists():
            value = installed_msi.get(repo)
            version = value[0].version if value else None
            installed = value[1]["source_sha"] if value and value[1] else None
            rows.append(_row(repo, "needs-clone", f"missing local clone: {clone}", oid, installed, version))
            continue
        try:
            validate_clone(root, repo)
        except InstallError as exc:
            rows.append(_row(repo, "error", str(exc), oid))
            continue
        try:
            msi_value = installed_msi.get(repo)
            if msi_value:
                product, msi_receipt = msi_value
                installed = msi_receipt["source_sha"] if msi_receipt else None
                version = product.version
                receipt = None
            else:
                receipt = read_receipt(repo)
                installed = receipt["sha"] if receipt else None
                version = None
        except (InstallError, msi.MsiError) as exc:
            rows.append(_row(repo, "error", str(exc), oid))
            continue
        if version is not None:
            detail = f"MSI {version} is installed" + (f" from {installed}" if installed else " (legacy provenance unknown)")
        else:
            detail = "fork and local clone are ready" if receipt is None else f"installed {installed}"
        rows.append(_row(repo, "ready", detail, oid, installed, version))
    return rows


def command_fork(root: Path, repo: str) -> None:
    _bootstrap(root, repo)
    print(f"prepared {ACCOUNT}/{repo}")


def _bootstrap(root: Path, repo: str) -> None:
    sibling = root / "storytold_forks.py"
    if not sibling.is_file():
        raise InstallError(f"missing bootstrap helper: {sibling}")
    _run([sys.executable, sibling, "--root", root, "bootstrap", repo], timeout=300.0)


def install_repo(root: Path, repo: str) -> str:
    """Prepare a missing fork/checkout/branch once, then build it safely."""
    try:
        row = status_rows(root, [repo])[0]
    except InstallError as exc:
        raise InstallError(f"status stage failed for {repo}: {exc}") from exc
    state = row["state"]
    if state == "error":
        raise InstallError(f"status stage failed for {repo}: {row['detail']}")
    if state in {"new", "needs-clone", "needs-branch"}:
        try:
            _bootstrap(root, repo)
        except InstallError as exc:
            raise InstallError(f"bootstrap stage failed for {repo}: {exc}") from exc
    elif state != "ready":
        raise InstallError(f"status stage failed for {repo}: unexpected state {state!r}")
    try:
        return build_repo(root, repo)
    except InstallError as exc:
        raise InstallError(f"build stage failed for {repo}: {exc}") from exc


def _host_target() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "aarch64-pc-windows-msvc"
    if machine in {"amd64", "x86_64"}:
        return "x86_64-pc-windows-msvc"
    raise InstallError(f"unsupported Windows architecture: {platform.machine()}")


def _is_windows() -> bool:
    return os.name == "nt"


def select_packages(
    metadata: dict[str, Any], repo: str, required_binaries: Sequence[str] | None = None
) -> tuple[list[str], list[str]]:
    packages = metadata.get("packages")
    if not isinstance(packages, list):
        raise InstallError("cargo metadata did not contain a package list")
    bins: dict[str, set[str]] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            continue
        targets = package.get("targets", [])
        bins[package["name"]] = {
            target.get("name")
            for target in targets
            if isinstance(target, dict) and "bin" in target.get("kind", []) and isinstance(target.get("name"), str)
        }
    if repo == LAUNCHER_REPO:
        required = {"artcraft-launcher": "artcraft-launcher", "artcraft-launcher-mcp": "craft-ai-mcp"}
        for package, binary in required.items():
            if binary not in bins.get(package, set()):
                raise InstallError(f"verified launcher recipe requires {package!r} package with {binary!r} binary")
        return list(required), list(required.values())
    required = list(required_binaries or [repo])
    if not required or required[0] != repo:
        raise InstallError(f"Windows recipe must name {repo!r} as its GUI binary")
    for binary in required:
        if binary not in bins or binary not in bins[binary]:
            raise InstallError(f"verified recipe requires package and binary named {binary!r}")
    selected = list(required)
    executables = list(required)
    cli = f"{repo}-cli"
    if cli not in selected and cli in bins and cli in bins[cli]:
        selected.append(cli)
        executables.append(cli)
    return selected, executables


def _windows_recipe_bins(source: Path, repo: str) -> list[str] | None:
    """Read the recipe's unconditional $Bins declaration when it has one.

    The public Craft recipes use this declaration as their runtime payload
    contract. Conditional ``$Bins +=`` entries remain optional; only names in
    the initial literal array are required.
    """
    script = source / "packaging" / "windows" / "package.ps1"
    if not script.is_file():
        return None
    try:
        text = script.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InstallError(f"could not read Windows packaging recipe {script}: {exc}") from exc
    match = re.search(r"(?im)^\s*\$Bins\s*=\s*(?:@\(([^)]*)\)|([^\r\n;]+))", text)
    if not match:
        return None
    declaration = match.group(1) if match.group(1) is not None else match.group(2)
    names = re.findall(r"['\"]([a-z0-9][a-z0-9-]*)['\"]", declaration)
    if not names or len(names) != len(set(names)) or names[0] != repo:
        raise InstallError(f"invalid $Bins declaration in {script}")
    return names


def _require_windows_recipe(source: Path, repo: str) -> list[str] | None:
    script = source / "packaging" / "windows" / "package.ps1"
    if not script.is_file():
        raise InstallError(f"{repo} source has no documented Windows packaging recipe: {script}")
    return _windows_recipe_bins(source, repo)


def _load_metadata(source: Path, env: dict[str, str], log: Any) -> dict[str, Any]:
    result = _run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps", "--locked"],
        cwd=source,
        env=env,
        check=False,
    )
    log.write("$ cargo metadata --format-version 1 --no-deps --locked\n")
    log.write(result.stdout)
    log.write(result.stderr)
    if result.returncode:
        raise InstallError(f"cargo metadata failed ({result.returncode})")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError("cargo metadata returned invalid JSON") from exc


def _cargo_build_argv(repo: str, packages: Sequence[str], target: str) -> list[str]:
    argv = ["cargo", "build", "--release", "--locked", "--target", target]
    for package_name in packages:
        argv.extend(["-p", package_name])
    features = FEATURES.get(repo)
    if features:
        argv.extend(["--features", features])
    return argv


def _stream_command(
    argv: Sequence[str], *, cwd: Path, env: dict[str, str], log: Any
) -> int:
    """Stream a long-running command directly to a durable log.

    The child inherits the log's OS handle, so Python never decodes or retains
    arbitrary compiler output. Short metadata commands use ``_run`` with
    replacement decoding because their structured stdout must be parsed.
    """
    log.flush()
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if _is_windows() else 0,
        )
        return process.wait()
    except OSError as exc:
        raise InstallError(f"could not run {argv[0]}: {exc}") from exc


def _copy_payload(source: Path, bin_dir: Path, stage: Path, binaries: Sequence[str]) -> list[str]:
    copied: list[str] = []
    for binary in binaries:
        name = f"{binary}.exe"
        src = bin_dir / name
        if not src.is_file():
            raise InstallError(f"Cargo succeeded but expected binary is missing: {src}")
        shutil.copy2(src, stage / name)
        copied.append(name)
    for name in NOTICE_NAMES:
        src = source / name
        if src.is_file():
            shutil.copy2(src, stage / name)
            copied.append(name)
    # Craft Anything embeds redistributable fonts.  Preserve the public OFL
    # notices named by its Windows packaging recipe without copying font data.
    font_notices = source / "assets" / "fonts"
    if font_notices.is_dir():
        for src in sorted(font_notices.glob("OFL-*.txt")):
            destination = stage / src.name
            shutil.copy2(src, destination)
            copied.append(src.name)
    # Public package recipes may provide explicitly named runtime payloads here.
    # Copy only files, never directories or wildcard build output.
    sidecar_manifest = source / "packaging" / "windows" / "storytold-sidecars.json"
    if sidecar_manifest.is_file():
        try:
            sidecars = json.loads(sidecar_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"invalid runtime sidecar manifest: {sidecar_manifest}: {exc}") from exc
        if not isinstance(sidecars, list) or any(not isinstance(x, str) for x in sidecars):
            raise InstallError(f"invalid runtime sidecar manifest: {sidecar_manifest}")
        for relative in sidecars:
            rel = Path(relative)
            if rel.is_absolute() or ".." in rel.parts:
                raise InstallError(f"unsafe runtime sidecar path: {relative}")
            src = source / rel
            if not src.is_file():
                raise InstallError(f"required runtime sidecar is missing: {src}")
            destination = stage / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, destination)
            copied.append(rel.as_posix())
    return copied


def _stage_pdfcraft_models(source: Path, stage: Path, env: dict[str, str], log: Any) -> list[str]:
    """Fetch and independently verify PdfCraft's manifest-declared OCR payload."""
    manifest_path = source / "ATTRIBUTION.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError(f"could not read PdfCraft model manifest {manifest_path}: {exc}") from exc
    fetched = manifest.get("fetched")
    if not isinstance(fetched, list):
        raise InstallError("PdfCraft ATTRIBUTION.toml has no fetched asset list")
    models: list[tuple[str, str, str]] = []
    for item in fetched:
        if not isinstance(item, dict) or item.get("kind") != "model":
            continue
        name = item.get("file")
        digest = item.get("sha256")
        licence_digest = item.get("licence_sha256")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(licence_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", licence_digest)
        ):
            raise InstallError("PdfCraft ATTRIBUTION.toml contains an invalid model entry")
        models.append((name, digest, licence_digest))
    if not models or len({name for name, _, _ in models}) != len(models):
        raise InstallError("PdfCraft ATTRIBUTION.toml has no unique model entries")

    models_dir = stage / "models"
    models_dir.mkdir()
    for partial in models_dir.glob("*.part"):
        partial.unlink()
    argv = ["cargo", "xtask", "models", os.fspath(models_dir)]
    log.write("$ " + " ".join(argv) + "\n")
    if _stream_command(argv, cwd=source, env=env, log=log):
        raise InstallError("cargo xtask models failed")
    for partial in models_dir.glob("*.part"):
        partial.unlink()

    copied = ["models/ATTRIBUTION.txt"]
    attribution = models_dir / "ATTRIBUTION.txt"
    if not attribution.is_file() or attribution.is_symlink() or not attribution.read_bytes().strip():
        raise InstallError("PdfCraft model attribution is missing or empty")
    for name, digest, licence_digest in models:
        model = models_dir / name
        licence = models_dir / f"{name}.LICENCE.txt"
        for path, expected, description in (
            (model, digest, "model"),
            (licence, licence_digest, "model licence"),
        ):
            if not path.is_file() or path.is_symlink():
                raise InstallError(f"PdfCraft {description} is missing or not a regular file: {path}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise InstallError(f"PdfCraft {description} failed ATTRIBUTION.toml verification: {path}")
        copied.extend([f"models/{name}", f"models/{name}.LICENCE.txt"])
    return copied


@contextlib.contextmanager
def repo_lock(repo: str, timeout: float = 120.0) -> Iterator[None]:
    directory = data_root() / repo
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".install.lock"
    deadline = time.monotonic() + timeout
    handle = lock.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    acquired = False
    while not acquired:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (OSError, BlockingIOError):
            if time.monotonic() >= deadline:
                handle.close()
                raise InstallError(f"timed out waiting for another {repo} operation")
            time.sleep(0.1)
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
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
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _verified_remote_sha(repo: str) -> str:
    node = query_forks([repo])[repo]
    if node is None:
        raise InstallError(f"{ACCOUNT}/{repo} is not the expected fork")
    sha = _validate_remote_node(repo, node, require_branch=True)
    assert sha is not None
    return sha


def _installed_msi(repo: str, *, required: bool = False) -> msi.InstalledProduct | None:
    if not _is_windows() or repo not in msi.UPGRADE_CODES:
        if required:
            raise InstallError(f"{repo} is not a supported installed MSI product")
        return None
    try:
        product = msi.inventory(repo)
    except msi.MsiError as exc:
        raise InstallError(str(exc)) from exc
    if product is None:
        if required:
            raise InstallError(f"no installed MSI in the pinned {repo} upgrade family")
        return None
    if _receipt_path(repo).exists():
        raise InstallError(f"{repo} has both MSI registration and a portable active receipt; refusing an ambiguous update")
    return product


def prepare_msi_repo(root: Path, repo: str) -> str:
    """Build and validate an MSI without changing Windows Installer state."""
    if not _is_windows():
        raise InstallError("prepare-msi is supported only on Windows")
    product = _installed_msi(repo, required=True)
    assert product is not None
    install_base = data_root() / repo
    logs = install_base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    target = "x86_64-pc-windows-msvc"
    target_dir = install_base / "msi-cargo-target"
    with repo_lock(repo):
        validate_clone(root, repo)
        remote_sha = _verified_remote_sha(repo)
        try:
            current_receipt = msi.read_receipt(data_root(), product)
        except msi.MsiError as exc:
            raise InstallError(str(exc)) from exc
        if current_receipt is not None and current_receipt["source_sha"] == remote_sha:
            return remote_sha
        try:
            msi.validate_prepared(data_root(), product, remote_sha)
        except msi.MsiError:
            pass
        else:
            return remote_sha
        log_path = logs / f"prepare-msi-{remote_sha}.log"
        source_parent = Path(tempfile.mkdtemp(prefix=f"storytold-{repo}-msi-source-"))
        source = source_parent / "source"
        dist = source_parent / "dist"
        try:
            with log_path.open("w", encoding="utf-8", errors="replace", newline="\n") as log:
                log.write(f"repository: {ACCOUNT}/{repo}\ncommit: {remote_sha}\nMSI update of: {product.product_code}\n")
                _run(["git", "init", source])
                _run(["git", "-C", source, "remote", "add", "origin", f"https://github.com/{ACCOUNT}/{repo}.git"])
                git_env = os.environ.copy()
                git_env["GIT_TERMINAL_PROMPT"] = "0"
                fetched = _run(
                    ["git", "-C", source, "fetch", "--depth=1", "origin", f"refs/heads/{BRANCH}"],
                    cwd=source_parent,
                    env=git_env,
                    check=False,
                    timeout=300.0,
                )
                log.write("$ git fetch --depth=1 origin refs/heads/chey\n" + fetched.stdout + fetched.stderr)
                if fetched.returncode:
                    raise InstallError(f"git fetch failed ({fetched.returncode})")
                fetched_sha = _run(["git", "-C", source, "rev-parse", "FETCH_HEAD"]).stdout.strip()
                if fetched_sha != remote_sha:
                    raise InstallError(f"fetched commit {fetched_sha} does not match verified remote commit {remote_sha}")
                _run(["git", "-C", source, "checkout", "--detach", remote_sha])
                script = source / "packaging" / "windows" / "package.ps1"
                declared = _require_windows_recipe(source, repo)
                env = msi.scrub_signing_env(os.environ)
                env["CARGO_TARGET_DIR"] = os.fspath(target_dir.resolve())
                env["DIST"] = os.fspath(dist.resolve())
                env["CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS"] = "-C target-feature=+crt-static"
                prefix = repo.replace("-", "_").upper()
                env[prefix + "_REQUIRE_WINRES"] = "1"
                env[prefix + "_BUILD_SHA"] = remote_sha
                metadata = _load_metadata(source, env, log)
                packages, binaries = select_packages(metadata, repo, declared)
                build_argv = _cargo_build_argv(repo, packages, target)
                log.write("$ " + " ".join(build_argv) + "\n")
                if _stream_command(build_argv, cwd=source, env=env, log=log):
                    raise InstallError("Cargo build failed")
                executable = target_dir / target / "release" / f"{binaries[0]}.exe"
                if not executable.is_file():
                    raise InstallError(f"Cargo succeeded but expected binary is missing: {executable}")
                candidates = []
                for wxs in (source / "packaging" / "windows").glob("*.wxs"):
                    try:
                        changed = msi.make_upgrade_rollback_safe(wxs, product.upgrade_code)
                    except msi.MsiError:
                        continue
                    candidates.append((wxs, changed))
                if len(candidates) != 1:
                    raise InstallError("Windows recipe does not have exactly one pinned Package UpgradeCode")
                log.write(f"rollback-safe WiX recipe: {candidates[0][0]} (edited={candidates[0][1]})\n")
                package_argv = ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", os.fspath(script), "-Arch", "x64", "-SkipBuild"]
                log.write("$ " + " ".join(package_argv) + "\n")
                if _stream_command(package_argv, cwd=source, env=env, log=log):
                    raise InstallError("Windows MSI packaging failed")
                packaged_executable = target_dir / "windows-package" / "x64" / f"{binaries[0]}.exe"
                if not packaged_executable.is_file():
                    raise InstallError(
                        f"packaging did not stage the expected executable payload: {packaged_executable}"
                    )
                packages_found = list(dist.glob("*.msi"))
                if len(packages_found) != 1:
                    raise InstallError(f"packaging produced {len(packages_found)} MSI files instead of one")
                try:
                    info = msi.native_api().inspect_package(packages_found[0])
                    msi.validate_package(info, product, repo)
                except msi.MsiError as exc:
                    raise InstallError(f"built MSI failed preflight: {exc}") from exc
                package_dir = install_base / "prepared" / remote_sha
                package_dir.mkdir(parents=True, exist_ok=True)
                persistent = package_dir / packages_found[0].name
                temporary = package_dir / (packages_found[0].name + ".tmp")
                shutil.copy2(packages_found[0], temporary)
                os.replace(temporary, persistent)
                prepared = {
                    "schema": 1,
                    "repo": repo,
                    "source_sha": remote_sha,
                    "installed": asdict(product),
                    "package": os.fspath(persistent.resolve()),
                    "package_sha256": hashlib.sha256(persistent.read_bytes()).hexdigest(),
                    "executable_sha256": hashlib.sha256(packaged_executable.read_bytes()).hexdigest(),
                }
                msi._atomic_json(msi.prepared_path(data_root(), repo), prepared)
        except (InstallError, OSError) as exc:
            raise InstallError(f"{exc}; prepare log: {log_path}") from exc
        finally:
            shutil.rmtree(source_parent, ignore_errors=True)
    return remote_sha


def _apply_prepared_msi(repo: str, source_sha: str) -> str:
    product = _installed_msi(repo, required=True)
    assert product is not None
    # MSI is a machine-wide transaction service.  Keep preparations parallel
    # per repository, but serialize the elevated application phase globally.
    with repo_lock("_windows-installer"), repo_lock(repo):
        try:
            return msi.install_prepared(data_root(), product, source_sha)
        except msi.MsiError as exc:
            raise InstallError(str(exc)) from exc


def build_repo(root: Path, repo: str) -> str:
    if not _is_windows():
        raise InstallError("build is supported only on Windows")
    if _installed_msi(repo) is not None:
        remote_sha = prepare_msi_repo(root, repo)
        current_product = _installed_msi(repo, required=True)
        assert current_product is not None
        try:
            current_receipt = msi.read_receipt(data_root(), current_product)
        except msi.MsiError as exc:
            raise InstallError(str(exc)) from exc
        if current_receipt is not None and current_receipt["source_sha"] == remote_sha:
            return remote_sha
        return _apply_prepared_msi(repo, remote_sha)
    install_base = data_root() / repo
    logs = install_base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    target = _host_target()
    target_dir = install_base / "cargo-target"
    versions = install_base / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    with repo_lock(repo):
        validate_clone(root, repo)
        remote_sha = _verified_remote_sha(repo)
        try:
            active = read_receipt(repo)
        except InstallError:
            # A forged, damaged, or legacy receipt is never a fast-path signal.
            # Rebuild into a fresh stage; activation remains atomic below.
            active = None
        if active is not None and active["sha"] == remote_sha:
            return remote_sha
        log_path = logs / f"build-{remote_sha}.log"
        source_parent = Path(tempfile.mkdtemp(prefix=f"storytold-{repo}-source-"))
        source = source_parent / "source"
        stage = install_base / "staging" / uuid.uuid4().hex
        stage.mkdir(parents=True)
        try:
            with log_path.open("w", encoding="utf-8", errors="replace", newline="\n") as log:
                log.write(f"repository: {ACCOUNT}/{repo}\ncommit: {remote_sha}\ntarget: {target}\n")
                _run(["git", "init", source])
                _run(["git", "-C", source, "remote", "add", "origin", f"https://github.com/{ACCOUNT}/{repo}.git"])
                git_env = os.environ.copy()
                git_env["GIT_TERMINAL_PROMPT"] = "0"
                fetch = _run(
                    ["git", "-C", source, "fetch", "--depth=1", "origin", f"refs/heads/{BRANCH}"],
                    check=False,
                    env=git_env,
                    timeout=300.0,
                )
                log.write("$ git fetch --depth=1 origin refs/heads/chey\n" + fetch.stdout + fetch.stderr)
                if fetch.returncode:
                    raise InstallError(f"git fetch failed ({fetch.returncode})")
                fetched = _run(["git", "-C", source, "rev-parse", "FETCH_HEAD"]).stdout.strip()
                if fetched != remote_sha:
                    raise InstallError(f"fetched commit {fetched} does not match verified remote commit {remote_sha}")
                _run(["git", "-C", source, "checkout", "--detach", remote_sha])
                staged_sha = _run(["git", "-C", source, "rev-parse", "HEAD"]).stdout.strip()
                if staged_sha != remote_sha:
                    raise InstallError(f"staged checkout {staged_sha} does not match verified remote commit {remote_sha}")
                log.write(f"staged checkout: {staged_sha}\n")
                env = os.environ.copy()
                env["CARGO_TARGET_DIR"] = str(target_dir)
                flag_name = "CARGO_TARGET_" + target.upper().replace("-", "_") + "_RUSTFLAGS"
                env[flag_name] = "-C target-feature=+crt-static"
                require_name = ("ARTCRAFT_LAUNCHER" if repo == LAUNCHER_REPO else repo.replace("-", "_").upper()) + "_REQUIRE_WINRES"
                env[require_name] = "1"
                build_prefix = "ARTCRAFT_LAUNCHER" if repo == LAUNCHER_REPO else repo.replace("-", "_").upper()
                env[build_prefix + "_BUILD_SHA"] = remote_sha
                metadata = _load_metadata(source, env, log)
                declared = _require_windows_recipe(source, repo)
                packages, binaries = select_packages(metadata, repo, declared)
                argv = _cargo_build_argv(repo, packages, target)
                log.write("$ " + " ".join(argv) + "\n")
                returncode = _stream_command(argv, cwd=source, env=env, log=log)
                if returncode:
                    raise InstallError(f"Cargo build failed ({returncode})")
                copied = _copy_payload(source, target_dir / target / "release", stage, binaries)
                if repo == "pdfcraft":
                    icon_licence = source / "assets" / "app-icon" / "LICENSE-document.txt"
                    if not icon_licence.is_file():
                        raise InstallError(f"PdfCraft document icon licence is missing: {icon_licence}")
                    shutil.copy2(icon_licence, stage / icon_licence.name)
                    copied.append(icon_licence.name)
                    copied.extend(_stage_pdfcraft_models(source, stage, env, log))
                hashes = {relative: hashlib.sha256((stage / relative).read_bytes()).hexdigest() for relative in copied}
                receipt = {
                    "schema": 1,
                    "repo": repo,
                    "sha": remote_sha,
                    "executable": f"{binaries[0]}.exe",
                    "files": copied,
                    "sha256": hashes,
                }
                (stage / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            version = versions / remote_sha
            if version.exists():
                existing = version / "receipt.json"
                try:
                    existing_value = json.loads(existing.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise InstallError(f"invalid immutable version receipt {existing}: {exc}") from exc
                if not isinstance(existing_value, dict):
                    raise InstallError(f"invalid immutable version receipt {existing}: expected an object")
                _validate_receipt_payload(repo, dict(existing_value), version, existing)
                if existing_value != receipt:
                    raise InstallError(f"immutable version directory already exists with different contents: {version}")
                shutil.rmtree(stage)
            else:
                stage.replace(version)
            _atomic_json(_receipt_path(repo), receipt)
        except (InstallError, OSError) as exc:
            raise InstallError(f"{exc}; build log: {log_path}") from exc
        finally:
            shutil.rmtree(source_parent, ignore_errors=True)
            shutil.rmtree(stage, ignore_errors=True)
    return remote_sha


def command_open(repo: str, app_args: Sequence[str] = ()) -> None:
    if not _is_windows():
        raise InstallError("open is supported only on Windows")
    product = _installed_msi(repo)
    if product is not None:
        executable = Path(product.executable)
    else:
        receipt = read_receipt(repo)
        if receipt is None:
            raise InstallError(f"{repo} is not installed")
        executable = Path(receipt["_exe"])
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    try:
        subprocess.Popen(
            [str(executable), *app_args],
            cwd=executable.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
    except OSError as exc:
        raise InstallError(f"could not launch {executable}: {exc}") from exc
    print(f"opened {repo}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("repos", nargs="+")
    for name in ("fork", "build", "install", "prepare-msi"):
        command = sub.add_parser(name)
        command.add_argument("repo")
    open_command = sub.add_parser("open")
    open_command.add_argument("repo")
    open_command.add_argument("app_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_argv)
    try:
        root = args.root.resolve()
        if args.command == "status":
            repos = [validate_slug(item) for item in args.repos]
            if len(set(repos)) != len(repos):
                raise InstallError("duplicate repository slug")
            print(json.dumps(status_rows(root, repos), separators=(",", ":")))
        else:
            repo = validate_slug(args.repo)
            if args.command == "fork":
                command_fork(root, repo)
            elif args.command == "build":
                sha = build_repo(root, repo)
                print(f"installed {repo} at {sha}")
            elif args.command == "install":
                sha = install_repo(root, repo)
                print(f"installed {repo} at {sha}")
            elif args.command == "prepare-msi":
                sha = prepare_msi_repo(root, repo)
                print(f"prepared MSI for {repo} at {sha}")
            else:
                separator = raw_argv.index("--") if "--" in raw_argv else None
                if separator is None and args.app_args:
                    raise InstallError("open application arguments require '--' separator")
                open_index = raw_argv.index("open")
                before_separator = raw_argv[open_index + 2 : separator] if separator is not None else []
                if before_separator:
                    raise InstallError("open application arguments must follow '--' separator")
                command_open(repo, raw_argv[separator + 1 :] if separator is not None else ())
        return 0
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
