"""Small, secret-free metadata snapshot for reproducing framework startup.

This module does not inspect Config, credentials, or environment variables.
It records installed versions and hashes of named dependency assets only.
"""

import hashlib
from importlib import metadata
from pathlib import Path
import platform
import re
import subprocess


EXECUTION_MODES = ("offline", "unit", "unit-xdist", "functional-collection")
_DISTRIBUTIONS = (
    "Faker", "PyYAML", "allure-pytest", "jsonpath-ng", "pytest", "pytest-dependency",
    "pytest-order", "pytest-rerunfailures", "pytest-timeout", "pytest-xdist", "requests",
)
_DEPENDENCY_FILES = (
    "Pipfile.lock", "constraints.txt", "pdm.lock", "poetry.lock", "pyproject.toml",
    "requirements-dev.txt", "requirements.lock", "requirements.txt", "uv.lock",
)


def _package_versions():
    versions = {}
    for name in _DISTRIBUTIONS:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
        except (OSError, ValueError):
            raise ValueError("Runtime manifest package metadata could not be read") from None
    return versions


def _dependency_digests(project_root):
    digests = {}
    for name in _DEPENDENCY_FILES:
        try:
            content = (project_root / name).read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError("Runtime manifest dependency asset could not be read") from None
        digests[name] = hashlib.sha256(content).hexdigest()
    return digests


def _git_sha(project_root):
    """Git is optional; accept a full local identity, never raw diagnostics."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=project_root, capture_output=True, text=True,
            encoding="ascii", errors="replace", timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return None
    value = result.stdout.strip()
    if re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value):
        return value.lower()
    return None


def _build_runtime_manifest(project_root, execution_mode):
    if execution_mode is not None and (
        not isinstance(execution_mode, str) or execution_mode not in EXECUTION_MODES
    ):
        raise ValueError("Runtime manifest execution mode is invalid")
    try:
        root = (Path(__file__).resolve().parents[1] if project_root is None
                else Path(project_root).resolve())
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ValueError("Runtime manifest project directory is invalid") from None
    return {
        "schema_version": 1,
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "versions": _package_versions(),
        "dependency_digests": _dependency_digests(root),
        "git_sha": _git_sha(root),
        "execution_mode": execution_mode,
    }


def build_runtime_manifest(project_root=None, *, execution_mode=None):
    """Return schema version 1 without secrets, absolute paths, or timestamps.

    ``execution_mode`` is an optional metadata label, not permission to execute
    functional tests. Missing distributions and unavailable Git are explicit
    nulls. A present but unreadable dependency asset is a safe startup error.
    """
    try:
        return _build_runtime_manifest(project_root, execution_mode)
    except ValueError as error:
        # Pytest renders public arguments even without --showlocals. The
        # private helper frames may also contain the supplied project path.
        # Drop both argument values before raising a new, unchained failure.
        project_root = execution_mode = None
        raise ValueError(str(error)) from None
