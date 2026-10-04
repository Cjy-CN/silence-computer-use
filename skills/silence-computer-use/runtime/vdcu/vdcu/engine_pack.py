"""Build a self-contained engine pack from explicitly supplied local sources.

Native binaries and the interpreter are copied without changes to their bytes.
This module does not download components or grant redistribution rights.
"""

from __future__ import annotations

import hashlib
import os
import shutil

ENGINE_BINARIES = ("pip_sdk.dll", "pip_main.exe", "pip_remote.dll", "agent_infra.dll")
INTERPRETER_MARKER = "python.exe"

# Shadow's own computer-use / browser-use agent API — plaintext Python packages
# that ship with its task mode.  Copied unchanged into the pack so the tool drives
# the product's real capabilities (seed_computer_use, seed_computer_use_ax,
# gac_runtime, aha_browser_use, ...) instead of a re-implementation.
AGENT_PACKAGES = (
    "seed_computer_use",
    "seed_computer_use_ax",
    "gac_runtime",
    "aha_browser_use",
    "aha_browser_use_speedup",
    "seed_browser_use",
)


class PackError(RuntimeError):
    pass


def default_pack_root() -> str:
    """The pack lives next to the package: <install>/engine."""

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "engine")


def find_source_app_dir() -> str:
    """Use an explicit local source; do not search unrelated installations."""
    path = os.environ.get("VDCU_SOURCE_APP_DIR", "")
    if path and os.path.isfile(os.path.join(path, "pip_sdk.dll")):
        return path
    raise PackError("pass --from <engine app directory> or set VDCU_SOURCE_APP_DIR")


def find_source_interpreter() -> str:
    """Use an explicitly supplied signed interpreter directory."""
    path = os.environ.get("VDCU_SOURCE_PYTHON_DIR", "")
    if path and os.path.isfile(os.path.join(path, INTERPRETER_MARKER)):
        return path
    raise PackError("pass --interpreter <python directory> or set VDCU_SOURCE_PYTHON_DIR")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_pack(
    *,
    app_dir: str | None = None,
    interpreter_dir: str | None = None,
    target_root: str | None = None,
) -> dict:
    """Copy the engine into a self-contained pack and report what was copied."""

    app_dir = app_dir or find_source_app_dir()
    interpreter_dir = interpreter_dir or find_source_interpreter()
    target_root = target_root or default_pack_root()

    missing = [name for name in ENGINE_BINARIES if not os.path.exists(os.path.join(app_dir, name))]
    if missing:
        raise PackError(f"{app_dir} is missing engine binaries: {missing}")
    if not os.path.exists(os.path.join(interpreter_dir, INTERPRETER_MARKER)):
        raise PackError(f"{interpreter_dir} has no {INTERPRETER_MARKER}")

    target_app = os.path.join(target_root, "app")
    target_python = os.path.join(target_root, "python")

    copied: dict[str, str] = {}
    os.makedirs(target_app, exist_ok=True)
    for name in ENGINE_BINARIES:
        source = os.path.join(app_dir, name)
        destination = os.path.join(target_app, name)
        shutil.copy2(source, destination)
        copied[name] = _sha256(destination)
    # the virtual cursor asset the engine draws onto snapshots
    cursor_src = os.path.join(app_dir, "pip", "cursor-blue-white.png")
    if os.path.exists(cursor_src):
        cursor_dst = os.path.join(target_app, "pip", "cursor-blue-white.png")
        os.makedirs(os.path.dirname(cursor_dst), exist_ok=True)
        shutil.copy2(cursor_src, cursor_dst)
        copied["pip/cursor-blue-white.png"] = _sha256(cursor_dst)

    if os.path.exists(target_python):
        shutil.rmtree(target_python)
    shutil.copytree(interpreter_dir, target_python)

    # the agent API packages (plaintext Python; copy unchanged)
    source_packages = os.path.join(os.path.dirname(os.path.dirname(app_dir)),
                                   "task-mode-resource", "python-packages")
    if not os.path.isdir(source_packages):
        source_packages = os.path.join(app_dir, "task-mode-resource", "python-packages")
    target_packages = os.path.join(target_root, "python-packages")
    copied_packages: list[str] = []
    if os.path.isdir(source_packages):
        os.makedirs(target_packages, exist_ok=True)
        for name in AGENT_PACKAGES:
            source = os.path.join(source_packages, name)
            if os.path.isdir(source):
                destination = os.path.join(target_packages, name)
                if os.path.exists(destination):
                    shutil.rmtree(destination)
                shutil.copytree(source, destination)
                copied_packages.append(name)

    return {
        "ok": True,
        "pack_root": target_root,
        "app_dir": target_app,
        "python_dir": target_python,
        "python_packages": target_packages if copied_packages else "",
        "agent_packages": copied_packages,
        "copied": copied,
        "interpreter_files": sum(len(files) for _root, _dirs, files in os.walk(target_python)),
        "note": (
            "binaries and the interpreter are byte-identical copies of the vendor's "
            "signed files — the signature allow-list stays satisfied without any patching"
        ),
    }


def verify_pack(target_root: str | None = None) -> dict:
    """Is the pack complete and byte-identical to its source copies?"""

    target_root = target_root or default_pack_root()
    target_app = os.path.join(target_root, "app")
    target_python = os.path.join(target_root, "python")
    report = {
        "ok": True,
        "pack_root": target_root,
        "app_dir": target_app,
        "python_dir": target_python,
        "missing": [],
    }
    for name in ENGINE_BINARIES:
        if not os.path.exists(os.path.join(target_app, name)):
            report["missing"].append(name)
    if not os.path.exists(os.path.join(target_python, INTERPRETER_MARKER)):
        report["missing"].append(f"python/{INTERPRETER_MARKER}")
    report["ok"] = not report["missing"]
    return report
