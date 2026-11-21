# Storytold forks on Windows

The Windows launcher uses the normal ArtCraft launcher interface—header, cards, table, details, toolbar, pinning, settings, menus, typography, and theme—when the Storytold helper is configured. There is no separate personal screen or mode switch.

Personal source mode is enabled only when `storytold_install.py` exists in the default root, or `STORYTOLD_FORKS_ROOT` is explicitly set. Without that configuration, Windows behaves like the ordinary launcher. Non-Windows and web builds are unchanged.

The default root is:

```text
E:\Users\cheyr\storytold forks
```

`STORYTOLD_PYTHON` can select another Python executable; the default is `python.exe`. The launcher invokes the sibling helper with `python -B` and the configured root.

The helper is the authority for official catalog repositories and the launcher repository (`craft-launcher`). Status reads the live full `origin/chey` commit and the managed install receipt. Cards may display a short commit, but actions are routed by validated catalog repository slug and the helper verifies the effective repository and branch.

- A missing fork has the existing **New** badge and a primary **Fork and install** button.
- A missing clone or `chey` branch uses the same install action; the helper completes bootstrap, clone, branch preparation, build, and install in one idempotent operation.
- Installed builds use the normal **Open** and **Update** card behavior.
- **Update all** queues installed personal builds one at a time and skips new repositories.
- Community apps remain visible for their information and links, but source-mode install management is disabled for them.

Mutations fail closed until a complete helper status response is known. Only one repository mutation runs at a time, helper work stays off the GUI thread, and later queued updates continue after a failure. Refresh is read-only. Errors are shown in the launcher and point to the launcher log for full diagnostics.

Official app releases are never installed from personal source mode: install, version picker, details, update-all, automatic-update, and final command dispatch all route through or are blocked by the personal adapter. The normal built-in launcher self-update surface remains available. After a helper-built Storytold Launcher is installed, fully quit the running launcher and start it again from the Storytold Launcher shortcut.
