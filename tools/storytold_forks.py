#!/usr/bin/env python3
"""Bootstrap and safely synchronize cheyras' Storytold forks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


OWNER = "cheyras"
UPSTREAM_OWNER = "storytold"
INITIAL_REPOS = (
    "anicraft",
    "texturecraft",
    "materialcraft",
    "craft-anything",
    "craft-launcher",
)
SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
AUTOMATION_NAME = "Storytold fork sync"
AUTOMATION_EMAIL = "storytold-fork-sync@users.noreply.github.com"


class SyncError(RuntimeError):
    """An expected, user-actionable synchronization failure."""


def command_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GH_PROMPT_DISABLED": "1",
        }
    )
    return env


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=command_env(),
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise SyncError(f"could not run {argv[0]}: {exc}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SyncError(f"{' '.join(argv)} failed: {detail or f'exit {result.returncode}'}")
    return result


def git(
    repo: Path, *args: str, check: bool = True, config: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    argv = ["git", "-C", str(repo)]
    # Automation must never execute a repository-controlled hook. /dev/null is
    # understood by Git for Windows as well as native Unix Git.
    for value in ("core.hooksPath=/dev/null", *config):
        argv.extend(("-c", value))
    return run([*argv, *args], check=check)


def output(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def validate_slug(name: str) -> str:
    if not SLUG_RE.fullmatch(name) or name in {".", ".."}:
        raise SyncError(f"invalid GitHub repository name: {name!r}")
    return name


def expected_urls(name: str) -> tuple[str, str]:
    return (
        f"https://github.com/{OWNER}/{name}.git",
        f"https://github.com/{UPSTREAM_OWNER}/{name}.git",
    )


def config_value(repo: Path, key: str) -> str | None:
    result = git(repo, "config", "--local", "--get", key, check=False)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise SyncError(f"cannot read {key} in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


def config_values(repo: Path, key: str) -> list[str]:
    result = git(repo, "config", "--local", "--get-all", key, check=False)
    if result.returncode == 1:
        return []
    if result.returncode:
        raise SyncError(f"cannot read {key} in {repo}: {result.stderr.strip()}")
    return result.stdout.splitlines()


def verify_clone(repo: Path, name: str) -> None:
    if not repo.is_dir() or git(repo, "rev-parse", "--is-inside-work-tree", check=False).returncode:
        raise SyncError(f"{repo} is not a Git working tree")
    top = git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    try:
        actual_top = Path(top).resolve(strict=True)
        expected_top = repo.resolve(strict=True)
    except OSError as exc:
        raise SyncError(f"cannot resolve worktree location for {repo}: {exc}") from exc
    if actual_top != expected_top:
        raise SyncError(
            f"{repo}: target is not the worktree top level (Git reports {actual_top})"
        )
    origin, upstream = expected_urls(name)
    actual_origins = config_values(repo, "remote.origin.url")
    actual_upstreams = config_values(repo, "remote.upstream.url")
    if actual_origins != [origin]:
        raise SyncError(
            f"{repo}: origin must have exactly one URL, {origin}; found {actual_origins or 'none'}"
        )
    if actual_upstreams != [upstream]:
        raise SyncError(
            f"{repo}: upstream must have exactly one URL, {upstream}; found {actual_upstreams or 'none'}"
        )
    for remote in ("origin", "upstream"):
        pushurls = config_values(repo, f"remote.{remote}.pushurl")
        if pushurls:
            raise SyncError(
                f"{repo}: remote {remote} must not have pushurl entries; found {pushurls}"
            )


def ref(repo: Path, name: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", "--quiet", name, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    result = git(repo, "merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in (0, 1):
        raise SyncError(f"cannot compare {older} and {newer}: {result.stderr.strip()}")
    return result.returncode == 0


def fetch(repo: Path, remote: str, branches: tuple[str, ...]) -> None:
    previous = {
        branch: ref(repo, f"refs/remotes/{remote}/{branch}") for branch in branches
    }
    specs = [
        f"refs/heads/{branch}:refs/remotes/{remote}/{branch}" for branch in branches
    ]
    result = git(repo, "fetch", "--no-tags", remote, *specs, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SyncError(
            f"fetch from {remote} was rejected (remote history may have been rewritten): {detail}"
        )

    rewritten: list[str] = []
    for branch, old in previous.items():
        new = ref(repo, f"refs/remotes/{remote}/{branch}")
        if old is not None and new is not None and not is_ancestor(repo, old, new):
            rewritten.append(branch)
    if rewritten:
        # Explicit non-head destinations can be rewound by fetch even without '+'.
        # Put every pre-fetch tracking ref back so the failed fetch is atomic locally.
        for branch, old in previous.items():
            if old is not None:
                git(repo, "update-ref", f"refs/remotes/{remote}/{branch}", old)
        names = ", ".join(f"{remote}/{branch}" for branch in rewritten)
        raise SyncError(
            f"refusing rewritten remote history for {names}; inspect the remote and recover manually"
        )


def github_is_real_endpoint(repo: Path, remote: str) -> bool:
    result = git(repo, "remote", "get-url", "--push", "--all", remote)
    urls = result.stdout.splitlines()
    if len(urls) != 1:
        raise SyncError(f"{repo}: {remote} resolves to an unexpected set of push endpoints: {urls}")
    configured = config_values(repo, f"remote.{remote}.url")
    return len(configured) == 1 and urls[0] == configured[0]


def push(repo: Path, source: str, branch: str) -> None:
    git_config: tuple[str, ...] = ()
    if github_is_real_endpoint(repo, "origin"):
        verify_gh_login()
        # Clear inherited helpers and use the credential for the verified gh account
        # for this command only. No token is placed in argv, config, or a URL.
        git_config = ("credential.helper=", "credential.helper=!gh auth git-credential")
    result = git(
        repo,
        "push",
        "origin",
        f"{source}:refs/heads/{branch}",
        check=False,
        config=git_config,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SyncError(
            f"origin/{branch} changed, authentication failed, or push was rejected; "
            f"confirm `gh auth status` is logged in as {OWNER}, then retry: {detail}"
        )


def make_merge_commit(repo: Path, chey_sha: str, main_sha: str) -> str:
    merged = git(repo, "merge-tree", "--write-tree", chey_sha, main_sha, check=False)
    if merged.returncode:
        detail = (merged.stdout + "\n" + merged.stderr).strip()
        raise SyncError(
            "chey conflicts with the new upstream/main; origin/main was updated but "
            f"origin/chey was left unchanged. Resolve manually. Git reported:\n{detail}"
        )
    tree = merged.stdout.splitlines()[0].strip() if merged.stdout else ""
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", tree):
        raise SyncError(f"git merge-tree returned an unexpected tree id: {tree!r}")
    message = f"Merge upstream/main into chey\n\nUpstream: {main_sha}\n"
    env = command_env()
    env.update(
        {
            "GIT_AUTHOR_NAME": AUTOMATION_NAME,
            "GIT_AUTHOR_EMAIL": AUTOMATION_EMAIL,
            "GIT_COMMITTER_NAME": AUTOMATION_NAME,
            "GIT_COMMITTER_EMAIL": AUTOMATION_EMAIL,
        }
    )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "commit-tree",
            tree,
            "-p",
            chey_sha,
            "-p",
            main_sha,
            "-F",
            "-",
        ],
        input=message,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if result.returncode:
        raise SyncError(f"could not create chey merge commit: {result.stderr.strip()}")
    return result.stdout.strip()


def worktrees(repo: Path) -> tuple[dict[str, Path], list[Path]]:
    result: dict[str, Path] = {}
    paths: list[Path] = []
    current_path: Path | None = None
    for line in output(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[9:])
            paths.append(current_path)
        elif line.startswith("branch refs/heads/") and current_path is not None:
            result[line.removeprefix("branch refs/heads/")] = current_path
    return result, paths


def clean_worktree(path: Path) -> bool:
    status = git(path, "status", "--porcelain", "--untracked-files=normal", check=False)
    return status.returncode == 0 and not status.stdout.strip()


def operation_in_progress(path: Path) -> bool:
    markers = (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "BISECT_START",
        "rebase-apply",
        "rebase-merge",
    )
    for marker in markers:
        result = git(path, "rev-parse", "--git-path", marker, check=False)
        if result.returncode:
            return True
        marker_path = Path(result.stdout.strip())
        if not marker_path.is_absolute():
            marker_path = path / marker_path
        if marker_path.exists():
            return True
    return False


def update_local_branch(repo: Path, branch: str, target: str, checked: dict[str, Path]) -> None:
    local_ref = f"refs/heads/{branch}"
    old = ref(repo, local_ref)
    if old == target:
        return
    if old is not None and not is_ancestor(repo, old, target):
        print(f"  kept local {branch}: it has fork-only or divergent commits")
        return
    path = checked.get(branch)
    if path is not None:
        if path.resolve() != repo.resolve():
            print(f"  kept local {branch}: it is checked out in another linked worktree")
            return
        if not clean_worktree(path):
            print(f"  kept local {branch}: its worktree has local changes or untracked files")
            return
        result = git(
            path,
            "merge",
            "--ff-only",
            target,
            check=False,
            config=("core.hooksPath=/dev/null",),
        )
        if result.returncode:
            print(f"  kept local {branch}: safe fast-forward failed: {result.stderr.strip()}")
            return
    else:
        args = ["update-ref", local_ref, target]
        if old is not None:
            args.append(old)
        git(repo, *args)
    git(repo, "config", f"branch.{branch}.remote", "origin")
    git(repo, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")


def sync_repo(repo: Path, name: str) -> None:
    verify_clone(repo, name)
    fetch(repo, "origin", ("main", "chey"))
    fetch(repo, "upstream", ("main",))
    upstream_main = ref(repo, "refs/remotes/upstream/main")
    origin_main = ref(repo, "refs/remotes/origin/main")
    if upstream_main is None:
        raise SyncError("upstream/main does not exist")
    if origin_main is None:
        raise SyncError("origin/main does not exist")
    if not is_ancestor(repo, origin_main, upstream_main):
        raise SyncError(
            "origin/main is ahead of or divergent from upstream/main; refusing to overwrite "
            "mirror history. Inspect both branches, preserve any personal commits on chey or a "
            "topic branch, and restore origin/main to the upstream lineage manually."
        )

    if origin_main != upstream_main:
        push(repo, upstream_main, "main")
        print(f"  advanced origin/main to {upstream_main[:12]}")

    origin_chey = ref(repo, "refs/remotes/origin/chey")
    if origin_chey is None:
        raise SyncError("origin/chey does not exist; run bootstrap to initialize it")
    chey_target = origin_chey
    if is_ancestor(repo, origin_chey, upstream_main):
        chey_target = upstream_main
    elif not is_ancestor(repo, upstream_main, origin_chey):
        chey_target = make_merge_commit(repo, origin_chey, upstream_main)
    if chey_target != origin_chey:
        push(repo, chey_target, "chey")
        print(f"  integrated upstream/main into origin/chey at {chey_target[:12]}")

    checked, associated = worktrees(repo)
    if any(operation_in_progress(path) for path in associated):
        print("  kept local main and chey: a linked worktree has a Git operation in progress")
        return
    update_local_branch(repo, "main", upstream_main, checked)
    update_local_branch(repo, "chey", chey_target, checked)


def gh_json(full_name: str, *, missing_ok: bool = False) -> dict[str, object] | None:
    # REST plus response headers lets us distinguish a genuine 404 from auth,
    # permission, rate-limit, and network failures. Only a proven 404 permits
    # bootstrap to create a fork.
    result = run(
        ["gh", "api", "--include", f"repos/{full_name}"],
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        explicit_404 = re.search(r"(?im)^HTTP/\S+\s+404(?:\s|$)", combined) is not None
        if missing_ok and explicit_404:
            return None
        raise SyncError(f"cannot inspect {full_name}: {detail}")
    body_start = result.stdout.find("{")
    if body_start < 0:
        raise SyncError(f"gh returned no JSON metadata for {full_name}")
    try:
        raw = json.loads(result.stdout[body_start:])
    except json.JSONDecodeError as exc:
        raise SyncError(f"gh returned invalid metadata for {full_name}") from exc
    if not isinstance(raw, dict):
        raise SyncError(f"gh returned unexpected metadata for {full_name}")

    parent = raw.get("parent")
    parent_name: object = None
    if isinstance(parent, dict):
        parent_name = parent.get("full_name") or parent.get("nameWithOwner")
    elif isinstance(parent, str):
        parent_name = parent
    return {
        "nameWithOwner": raw.get("full_name") or raw.get("nameWithOwner"),
        "defaultBranchRef": {"name": raw.get("default_branch")}
        if isinstance(raw.get("default_branch"), str)
        else raw.get("defaultBranchRef"),
        "isFork": raw.get("fork") if "fork" in raw else raw.get("isFork"),
        "parent": {"nameWithOwner": parent_name} if parent_name is not None else parent,
    }


def default_branch(meta: dict[str, object]) -> str | None:
    value = meta.get("defaultBranchRef")
    return value.get("name") if isinstance(value, dict) and isinstance(value.get("name"), str) else None


def repository_identity(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    direct = value.get("nameWithOwner") or value.get("full_name")
    if isinstance(direct, str):
        return direct
    name = value.get("name")
    owner = value.get("owner")
    if isinstance(owner, dict):
        owner = owner.get("login") or owner.get("name")
    if isinstance(owner, str) and isinstance(name, str):
        return f"{owner}/{name}"
    return None


def validate_upstream_metadata(name: str, meta: dict[str, object]) -> None:
    expected = f"{UPSTREAM_OWNER}/{name}"
    actual = repository_identity(meta)
    if (
        not isinstance(actual, str)
        or actual.casefold() != expected.casefold()
        or default_branch(meta) != "main"
    ):
        raise SyncError(f"{expected} must exist with main as its default branch")


def validate_fork_metadata(name: str, meta: dict[str, object]) -> None:
    expected = f"{OWNER}/{name}"
    parent_name = repository_identity(meta.get("parent"))
    actual = repository_identity(meta)
    if (
        not isinstance(actual, str)
        or actual.casefold() != expected.casefold()
        or meta.get("isFork") is not True
        or not isinstance(parent_name, str)
        or parent_name.casefold() != f"{UPSTREAM_OWNER}/{name}".casefold()
        or default_branch(meta) != "main"
    ):
        raise SyncError(
            f"{expected} must be a fork of {UPSTREAM_OWNER}/{name} with default branch main"
        )


def verify_gh_login() -> None:
    result = run(["gh", "api", "user", "--jq", ".login"], check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SyncError(
            f"GitHub authentication check failed; run `gh auth login` as {OWNER} and retry: {detail}"
        )
    if result.stdout.strip().casefold() != OWNER.casefold():
        raise SyncError(
            f"active gh login must be {OWNER}; found {result.stdout.strip() or 'no authenticated user'}"
        )


def wait_for_fork(name: str, timeout: float = 60.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    url, _ = expected_urls(name)
    last_error = "fork not visible"
    while time.monotonic() < deadline:
        try:
            meta = gh_json(f"{OWNER}/{name}", missing_ok=True)
            if meta is not None:
                validate_fork_metadata(name, meta)
                probe = run(["git", "ls-remote", "--exit-code", url, "refs/heads/main"], check=False)
                if probe.returncode == 0:
                    return meta
                last_error = (probe.stderr or probe.stdout).strip() or "main ref not visible"
        except SyncError as exc:
            last_error = str(exc)
        time.sleep(2)
    raise SyncError(f"timed out waiting for {OWNER}/{name}: {last_error}")


def bootstrap(root: Path, name: str) -> None:
    name = validate_slug(name)
    if root.exists() and not root.is_dir():
        raise SyncError(f"repository root is not a directory: {root}")
    repo = root / name
    if repo.exists() and (not repo.is_dir() or not (repo / ".git").exists()):
        raise SyncError(f"refusing name collision at {repo}")

    verify_gh_login()
    upstream_meta = gh_json(f"{UPSTREAM_OWNER}/{name}")
    assert upstream_meta is not None
    validate_upstream_metadata(name, upstream_meta)
    fork_meta = gh_json(f"{OWNER}/{name}", missing_ok=True)
    if fork_meta is None:
        if repo.exists():
            raise SyncError(f"refusing to create a remote fork while an existing clone occupies {repo}")
        run(
            [
                "gh",
                "repo",
                "fork",
                f"{UPSTREAM_OWNER}/{name}",
                "--default-branch-only",
                "--clone=false",
            ]
        )
        fork_meta = wait_for_fork(name)
    validate_fork_metadata(name, fork_meta)

    new_clone = not repo.exists()
    if new_clone:
        root.mkdir(parents=True, exist_ok=True)
        origin_url, upstream_url = expected_urls(name)
        run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--origin",
                "origin",
                origin_url,
                str(repo),
            ]
        )
        git(repo, "remote", "add", "upstream", upstream_url)
    else:
        verify_clone(repo, name)

    # A new fork does not have chey until bootstrap creates it.
    fetch(repo, "origin", ("main",))
    fetch(repo, "upstream", ("main",))
    upstream_main = ref(repo, "refs/remotes/upstream/main")
    origin_main = ref(repo, "refs/remotes/origin/main")
    if upstream_main is None or origin_main is None:
        raise SyncError("both origin/main and upstream/main are required")
    if not is_ancestor(repo, origin_main, upstream_main):
        raise SyncError("origin/main is not a fast-forward ancestor of upstream/main")
    if origin_main != upstream_main:
        push(repo, upstream_main, "main")

    origin_chey = ref(repo, "refs/remotes/origin/chey")
    if origin_chey is None:
        push(repo, upstream_main, "chey")
        origin_chey = upstream_main
    # A push whose source is a raw object id is not guaranteed to update the
    # remote-tracking ref. Refresh before creating a tracking local branch.
    fetch(repo, "origin", ("main", "chey"))
    sync_repo(repo, name)

    if new_clone:
        git(repo, "switch", "chey")

    git(repo, "config", "--local", "storytold.cheyManaged", "true")
    print(f"bootstrapped {name}; checked out chey" if new_clone else f"enrolled {name}")


def enrolled_repos(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    names: list[str] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if not child.is_dir():
            continue
        marker = config_value(child, "storytold.cheyManaged") if (child / ".git").exists() else None
        if marker is not None and marker.casefold() in {"true", "yes", "on", "1"}:
            names.append(child.name)
    return names


def sync_many(root: Path, requested: list[str]) -> int:
    names = requested if requested else enrolled_repos(root)
    failures = 0
    for candidate in names:
        print(f"syncing {candidate}")
        try:
            name = validate_slug(candidate)
            sync_repo(root / name, name)
            print(f"  {name}: ok")
        except SyncError as exc:
            failures += 1
            print(f"  {candidate}: ERROR: {exc}", file=sys.stderr)
    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    commands = parser.add_subparsers(dest="command", required=True)
    sync_parser = commands.add_parser("sync", help="synchronize enrolled or named repositories")
    sync_parser.add_argument("repos", nargs="*", metavar="REPO")
    bootstrap_parser = commands.add_parser("bootstrap", help="create and enroll one fork")
    bootstrap_parser.add_argument("repo", metavar="REPO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        if args.command == "bootstrap":
            bootstrap(root, args.repo)
            return 0
        return sync_many(root, args.repos)
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
