# Personal forks on Windows

The Windows desktop launcher has a **Personal forks / Official releases** switch. Personal forks is the default when `storytold_install.py` exists in the configured personal root, or when `STORYTOLD_FORKS_ROOT` is explicitly set. Official releases keeps the normal signed ArtCraft release view and trust rules.

The default personal root is:

```text
E:\Users\cheyr\storytold forks
```

When setting up elsewhere, copy both `tools/storytold_forks.py` and `tools/storytold_install.py` into the parent directory, then put them at `E:\Users\cheyr\storytold forks\storytold_forks.py` and `E:\Users\cheyr\storytold forks\storytold_install.py`. Set `STORYTOLD_FORKS_ROOT` to use another root and `STORYTOLD_PYTHON` to use another Python executable; the default executable is `python.exe`.

Personal rows come from the live, verified launcher catalog's official `storytold/` repositories, plus the launcher repository `craft-launcher`. The helper reports and operates on the owner's `origin/chey` branch. Community repositories are not included. Refresh only reads status and never installs or updates anything.

Fork, Clone, Create chey branch, Install from chey, and Update from chey are explicit actions. Personal builds install side by side according to the helper's per-repository build and install rules. The displayed commit identifies the `origin/chey` source and the installed commit so a row can distinguish Install from chey, Update from chey, and Up to date. Open launches an installed personal app through the helper.

The supported Windows source-build recipes are AniCraft, TextureCraft, MaterialCraft, and Craft Anything, plus the Storytold Launcher. Other official catalog apps can still be discovered and forked, but may need a Windows build recipe before they can be built locally.

Automatic official installations are suspended in Personal forks mode. Use Official releases explicitly to see and manage signed upstream releases.

After installing or updating the launcher itself, fully exit the running launcher, then reopen it using the **Storytold Launcher** shortcut. The current process does not open the replacement in place.
