"""Cron: import path of the restart-safe external worker.

The worker is spawned as ``sys.executable -m cron.scheduler``. Its entry module is
``cron.scheduler``, not ``hermes_cli.main``, so nothing bootstraps the gateway's checkout
onto its ``sys.path``; historically it imported ``cron`` only through the implicit ``-m``
cwd entry. That entry is gone under ``PYTHONSAFEPATH`` and useless when the venv's
editable install maps a moved/deleted checkout -- the worker then dies with
"No module named 'cron'" before its ownership ack (#112729, hypothesised cause).

The shared subprocess sanitizer strips Hermes-owned PYTHONPATH entries because user
children must not see our tree. This child IS Hermes, so the pin is applied *after* the
env is built, on the sanitized env -- the sanitizer's other decisions (dropped runtime
site-packages, dropped venv markers) stand.
"""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path


def _installed_purelib() -> Path | None:
    try:
        return Path(sysconfig.get_paths()["purelib"]).resolve()
    except (KeyError, OSError):
        return None


def _runtime_site_packages(repo_root: Path) -> Path | None:
    """The Hermes dependency-environment site-packages PM selected for *repo_root*.

    Returns ``None`` when there is no committed PM dependency environment, when the call
    itself fails (never break the pin), or when that site-packages is already the running
    interpreter's own ``purelib`` (the worker IS the generation interpreter, so appending
    it would hoist site-packages ahead of the stdlib on ``sys.path`` -- the exact hazard
    ``_installed_purelib`` guards against for the repo root).
    """
    try:
        from pm.environments import selected_venv, site_packages
    except Exception:
        return None
    try:
        venv = selected_venv(repo_root)
        sp = Path(site_packages(venv))
    except Exception:
        return None
    installed = _installed_purelib()
    if installed is not None and sp.resolve() == installed.resolve():
        return None
    return sp if sp.is_dir() else None


def pin_hermes_tree_on_pythonpath(worker_env: dict, repo_root: Path) -> dict:
    """Prepend ``repo_root`` to the worker env's own PYTHONPATH (never ``os.environ``'s).

    Skipped when ``repo_root`` is the interpreter's ``purelib``: under a wheel / pipx /
    uv-tool install ``cron/`` lives in site-packages itself, which is already importable,
    and pinning it would move site-packages ahead of the stdlib on ``sys.path``.

    Otherwise, after pinning ``repo_root`` we also append the PM generation's site-packages
    (guarded by ``_runtime_site_packages``). The worker runs as ``sys.executable -m
    cron.scheduler`` where ``sys.executable`` may be a bare wrapper Python (the multiplexed
    gateway's tool interpreter) whose own site-packages lack the checked-in dependencies --
    e.g. ``ruamel.yaml`` (pyproject, pinned for python>=3.14) -- that only live in the PM
    generation PM committed for this checkout. ``repo_root`` stays first so ``cron``
    resolves to this checkout; the generation site-packages supplies the runtime deps it
    needs to import.
    """
    root = str(repo_root)
    if _installed_purelib() == Path(root).resolve():
        return worker_env
    pin = [root]
    runtime_sp = _runtime_site_packages(repo_root)
    if runtime_sp is not None:
        pin.append(str(runtime_sp))
    existing = [e for e in worker_env.get("PYTHONPATH", "").split(os.pathsep) if e]
    worker_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([*pin, *existing]))
    return worker_env
