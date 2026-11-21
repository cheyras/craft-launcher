# Storytold forks on Windows

The Windows launcher uses the normal ArtCraft launcher interface—header, cards, table, details, toolbar, pinning, settings, menus, typography, and theme—when the Storytold helper is configured. There is no separate personal screen or mode switch.

Personal source mode is enabled only when `storytold_install.py` exists in the default root, or `STORYTOLD_FORKS_ROOT` is explicitly set. Without that configuration, Windows behaves like the ordinary launcher. Non-Windows and web builds are unchanged.

The default root is:

```text
E:\Users\cheyr\storytold forks
```

`STORYTOLD_PYTHON` can select another Python executable; the default is `python.exe`. The launcher invokes the sibling helper with `python -B` and the configured root.

The helper is the authority for official catalog repositories and the launcher repository (`craft-launcher`). Status reads the live full `origin/chey` commit and the managed install receipt. Cards may display a short commit, but actions are routed by validated catalog repository slug and the helper verifies the effective repository and branch.

The twelve established Windows products (`cadcraft`, `deckcraft`, `designcraft`, `effectcraft`, `filmcraft`, `gridcraft`, `lightcraft`, `pdfcraft`, `photocraft`, `soundcraft`, `vectorcraft`, and `wordcraft`) retain their existing Windows Installer identity. The helper inventories their fixed UpgradeCode and HKLM App Paths registration, so a legacy per-machine x64 install remains visible and opens even when offline and before it has a managed source receipt. Unknown or conflicting MSI scope, registrations, paths, or a simultaneous portable receipt are errors rather than reasons to create another copy.

For these products, **Install**, **Update**, and the existing **Build** command use a Windows Installer major upgrade in the registered folder. They never manually uninstall the old product. The helper builds the exact verified `cheyras/<repo>` `chey` commit, runs the repository's complete WiX recipe with signing credentials removed, checks upgrade identity, version, scope, architecture, rollback scheduling, and payload through read-only MSI APIs, and invokes native `msiexec` with passive UI and no forced restart. Activation is recorded only after the old registration is gone, the new product is unique, and the registered executable equals the verified packaged bytes.

`prepare-msi REPO` performs the source build, packaging, and preflight only. It writes a hash-bound prepared package to the per-user Storytold cache and does not invoke Windows Installer. This permits the twelve builds to be prepared independently; ordinary `install REPO` or `build REPO` later revalidates the source commit, package hash, current ProductCode, scope, path, and version immediately before the serial elevated install. If the machine is offline or installation state changed, application stops before mutation and preparation must be repeated. Durable preparation and MSI logs remain under `%LOCALAPPDATA%\StorytoldForks\<repo>` for safe diagnosis and retry.

- A missing fork has the existing **New** badge and a primary **Fork and install** button.
- A missing clone or `chey` branch uses the same install action; the helper completes bootstrap, clone, branch preparation, build, and install in one idempotent operation.
- Installed builds use the normal **Open** and **Update** card behavior.
- **Update all** queues installed personal builds one at a time and skips new repositories.
- Community apps remain visible for their information and links, but source-mode install management is disabled for them.

Mutations fail closed until a complete helper status response is known. Only one repository mutation runs at a time, helper work stays off the GUI thread, and later queued updates continue after a failure. Refresh is read-only. Errors are shown in the launcher and point to the launcher log for full diagnostics.

Official app releases are never installed from personal source mode: install, version picker, details, update-all, automatic-update, and final command dispatch all route through or are blocked by the personal adapter. The normal built-in launcher self-update surface remains available. After a helper-built Storytold Launcher is installed, fully quit the running launcher and start it again from the Storytold Launcher shortcut.
