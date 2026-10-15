"""The reproducibility manifest exposes metadata, never private startup input."""

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from wizbank_api_test.utils import runtime_manifest as module


SECRET = "SYNTHETIC_B4_MANIFEST_PASSWORD_TOKEN_COOKIE"
ROOT = Path(__file__).resolve().parents[2]
VERSIONS = {
    "pytest", "pytest-xdist", "allure-pytest", "pytest-dependency", "pytest-order",
    "pytest-rerunfailures", "pytest-timeout", "requests", "PyYAML", "jsonpath-ng", "Faker",
}
DEPENDENCY_FILES = {
    "requirements.txt", "requirements-dev.txt", "constraints.txt", "pyproject.toml",
    "poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock", "requirements.lock",
}


@pytest.fixture(autouse=True)
def reject_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Real HTTP/network is forbidden in B4 manifest tests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def fake_git(monkeypatch, value="a" * 40):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=value + "\n", stderr=SECRET)

    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


def test_stable_schema_uses_actual_runtime_versions_and_no_timestamp(tmp_path, monkeypatch):
    fake_git(monkeypatch)
    manifest = module.build_runtime_manifest(tmp_path)

    assert set(manifest) == {
        "schema_version", "python", "platform", "versions", "dependency_digests",
        "git_sha", "execution_mode",
    }
    assert manifest["schema_version"] == 1
    assert manifest["python"] == {
        "implementation": platform.python_implementation(), "version": platform.python_version(),
    }
    assert manifest["platform"] == {"system": platform.system(), "machine": platform.machine()}
    assert set(manifest["versions"]) == VERSIONS
    for name in VERSIONS:
        try:
            expected = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            expected = None
        assert manifest["versions"][name] == expected
    assert manifest["dependency_digests"] == {}
    assert manifest["git_sha"] == "a" * 40
    assert manifest["execution_mode"] is None
    assert manifest == module.build_runtime_manifest(tmp_path)


def test_missing_distribution_is_an_explicit_null_not_an_environment_dump(tmp_path, monkeypatch):
    def unavailable(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(module.metadata, "version", unavailable)
    manifest = module.build_runtime_manifest(tmp_path)
    assert manifest["versions"] == dict.fromkeys(sorted(VERSIONS))
    assert SECRET not in json.dumps(manifest)


@pytest.mark.parametrize("filename", sorted(DEPENDENCY_FILES))
def test_dependency_digests_use_exact_raw_bytes_and_logical_filenames(tmp_path, filename):
    # requirements.txt in the baseline is UTF-16: no decoding or reserialization.
    raw = ("requests==2.32.4\r\n# 中文依赖\r\n" + SECRET).encode("utf-16")
    path = tmp_path / filename
    path.write_bytes(raw)
    first = module.build_runtime_manifest(tmp_path)
    assert first["dependency_digests"] == {filename: hashlib.sha256(raw).hexdigest()}
    assert SECRET not in json.dumps(first)
    path.write_bytes(raw + b"\x00")
    second = module.build_runtime_manifest(tmp_path)
    assert first["dependency_digests"] != second["dependency_digests"]


def test_manifest_never_reads_private_config_or_unbounded_requirement_names(tmp_path, monkeypatch):
    private = tmp_path / "configs"
    private.mkdir()
    (private / "env.private.yaml").write_text("password: " + SECRET, encoding="utf-8")
    (tmp_path / ("requirements-" + SECRET + ".txt")).write_text(SECRET, encoding="utf-8")
    monkeypatch.setenv("B4_PRIVATE_PASSWORD", SECRET)
    monkeypatch.setenv("B4_PRIVATE_HOME", str(tmp_path))
    real_read_bytes = Path.read_bytes

    def permitted(path):
        assert path.parent == tmp_path and path.name in DEPENDENCY_FILES
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", permitted)
    manifest = module.build_runtime_manifest(tmp_path)
    serialized = json.dumps(manifest)
    assert SECRET not in serialized
    assert str(tmp_path) not in serialized
    assert "configs" not in serialized
    assert manifest["dependency_digests"] == {}


@pytest.mark.parametrize("sha", ["a" * 40, "b" * 64])
def test_git_sha_accepts_only_local_full_hex_identity(tmp_path, monkeypatch, sha):
    calls = fake_git(monkeypatch, sha)
    assert module.build_runtime_manifest(tmp_path)["git_sha"] == sha
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == ["git", "rev-parse", "--verify", "HEAD"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs.get("shell", False) is False
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] > 0


@pytest.mark.parametrize(
    "output", ["", "a" * 39, "b" * 41, "g" * 40, SECRET, "a" * 40 + "\n" + SECRET],
    ids=["empty", "short", "long", "nonhex", "private", "multiline"],
)
def test_untrusted_git_output_is_not_exposed(tmp_path, monkeypatch, output):
    fake_git(monkeypatch, output)
    manifest = module.build_runtime_manifest(tmp_path)
    assert manifest["git_sha"] is None
    assert SECRET not in json.dumps(manifest)


@pytest.mark.parametrize("error", [FileNotFoundError(SECRET), subprocess.TimeoutExpired("git", 1, SECRET)])
def test_missing_or_unavailable_git_is_stably_null(tmp_path, monkeypatch, error):
    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(module.subprocess, "run", unavailable)
    assert module.build_runtime_manifest(tmp_path)["git_sha"] is None


def test_git_nonzero_output_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=128, stdout="a" * 40, stderr=SECRET,
    ))
    assert module.build_runtime_manifest(tmp_path)["git_sha"] is None


@pytest.mark.parametrize("mode", ["offline", "unit", "unit-xdist", "functional-collection"])
def test_execution_mode_is_an_explicit_safe_metadata_label(tmp_path, mode):
    manifest = module.build_runtime_manifest(tmp_path, execution_mode=mode)
    assert manifest["execution_mode"] == mode


@pytest.mark.parametrize(
    "mode", [SECRET, "", "real-login", [], 1],
    ids=["private", "empty", "unknown", "list", "number"],
)
def test_invalid_execution_mode_is_rejected_without_echoing_input(tmp_path, mode):
    with pytest.raises(ValueError) as error:
        module.build_runtime_manifest(tmp_path, execution_mode=mode)
    assert SECRET not in str(error.value)
    assert "execution mode" in str(error.value).lower()


def test_unreadable_dependency_digest_fails_safely_instead_of_ignoring_it(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("pytest", encoding="utf-8")

    def unavailable(*args, **kwargs):
        raise PermissionError(SECRET + str(tmp_path))

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    with pytest.raises(ValueError) as error:
        module.build_runtime_manifest(tmp_path)
    assert SECRET not in str(error.value)
    assert str(tmp_path) not in str(error.value)
    assert error.value.__suppress_context__


def test_corrupted_metadata_fails_safely_instead_of_becoming_a_missing_package(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError(SECRET + str(tmp_path))

    monkeypatch.setattr(module.metadata, "version", unavailable)
    with pytest.raises(ValueError) as error:
        module.build_runtime_manifest(tmp_path)
    assert SECRET not in str(error.value)
    assert str(tmp_path) not in str(error.value)
    assert error.value.__suppress_context__


@pytest.fixture
def standalone_cli(tmp_path):
    project = tmp_path / "项目 空格 checkout"
    (project / "scripts").mkdir(parents=True)
    (project / "utils").mkdir()
    for relative in ("scripts/runtime_manifest.py", "utils/runtime_manifest.py", "utils/__init__.py"):
        shutil.copyfile(ROOT / relative, project / relative)
    (project / "requirements.txt").write_text("pytest==8.4.1\n# 中文", encoding="utf-16")
    (project / "configs").mkdir()
    (project / "configs/env.private.yaml").write_text("password: " + SECRET, encoding="utf-8")
    cwd = tmp_path / "另一个 工作目录"
    cwd.mkdir()
    guard = tmp_path / "network guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import socket\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise RuntimeError('B4 subprocess network forbidden')\n"
        "socket.socket.connect = forbidden\n"
        "socket.socket.connect_ex = forbidden\n"
        "socket.create_connection = forbidden\n", encoding="utf-8",
    )
    env = os.environ.copy()
    # No project/package alias in PYTHONPATH: only test-level network enforcement.
    # Preserve an external validation sitecustomize guard, when one is supplied.
    guards = [part for part in env.get("PYTHONPATH", "").split(os.pathsep)
              if part and (Path(part) / "sitecustomize.py").is_file()]
    env["PYTHONPATH"] = os.pathsep.join([*guards, str(guard)])
    env["B4_PRIVATE_PASSWORD"] = SECRET
    return project, cwd, env


def run_cli(standalone_cli, *arguments):
    project, cwd, env = standalone_cli
    return subprocess.run(
        [sys.executable, str(project / "scripts/runtime_manifest.py"), *arguments],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_cli_bootstraps_from_unicode_space_checkout_and_different_cwd(standalone_cli):
    first = run_cli(standalone_cli, "--execution-mode", "offline")
    second = run_cli(standalone_cli, "--execution-mode", "offline")
    assert first.returncode == second.returncode == 0, first.stderr
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    manifest = json.loads(first.stdout)
    assert manifest["execution_mode"] == "offline"
    assert "requirements.txt" in manifest["dependency_digests"]
    assert SECRET not in first.stdout
    assert str(standalone_cli[0]) not in first.stdout
    assert str(standalone_cli[1]) not in first.stdout


@pytest.mark.parametrize("arguments", [("--execution-mode", SECRET), ("--" + SECRET,), (SECRET,), ("--execution-mode",)])
def test_cli_usage_errors_are_stable_and_do_not_echo_private_input(standalone_cli, arguments):
    result = run_cli(standalone_cli, *arguments)
    assert result.returncode == 2
    assert result.stdout == ""
    assert SECRET not in result.stderr
    assert str(standalone_cli[0]) not in result.stderr
    assert "error" in result.stderr.lower()


def test_cli_internal_startup_failure_has_fixed_safe_exit(standalone_cli):
    project, _, _ = standalone_cli
    # A malformed dependency asset is an error, never silently omitted.
    (project / "requirements.txt").unlink()
    (project / "requirements.txt").mkdir()
    result = run_cli(standalone_cli)
    assert result.returncode == 1
    assert result.stdout == ""
    assert SECRET not in result.stderr
    assert str(project) not in result.stderr
    assert "error" in result.stderr.lower()


def test_cli_help_does_not_require_runtime_metadata_or_private_config(standalone_cli):
    result = run_cli(standalone_cli, "--help")
    assert result.returncode == 0
    assert result.stderr == ""
    assert "--execution-mode" in result.stdout
    assert SECRET not in result.stdout
    assert str(standalone_cli[0]) not in result.stdout


@pytest.mark.parametrize("failure", ["dependency", "metadata", "mode", "project-path"])
def test_public_failure_does_not_expose_private_arguments_in_pytest_output(tmp_path, failure):
    private_root = tmp_path / "assets" / "SYNTHETIC_PATHSECRET"
    private_root.mkdir(parents=True)
    (private_root / "private.txt").write_text(SECRET, encoding="utf-8")
    (private_root / "requirements.txt").mkdir()
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\naddopts =\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "import socket\nimport pytest\nimport requests\n"
        "@pytest.fixture(autouse=True)\n"
        "def reject_network(monkeypatch):\n"
        "    def forbidden(*args, **kwargs):\n"
        "        pytest.fail('Real HTTP/network is forbidden in nested B4 tests')\n"
        "    monkeypatch.setattr(socket.socket, 'connect', forbidden)\n"
        "    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)\n"
        "    monkeypatch.setattr(socket, 'create_connection', forbidden)\n"
        "    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)\n",
        encoding="utf-8",
    )
    calls = {
        "dependency": "module.build_runtime_manifest(private_root)",
        "metadata": (
            "def broken_metadata(name):\n"
            "        raise OSError((private_root / 'private.txt').read_text(encoding='utf-8'))\n"
            "    monkeypatch.setattr(module.metadata, 'version', broken_metadata)\n"
            "    module.build_runtime_manifest(private_root)"
        ),
        "mode": (
            "module.build_runtime_manifest(private_root, execution_mode="
            "(private_root / 'private.txt').read_text(encoding='utf-8'))"
        ),
        "project-path": (
            "class PrivatePath:\n"
            "        def __fspath__(self):\n"
            "            raise ValueError((private_root / 'private.txt').read_text(encoding='utf-8'))\n"
            "        def __repr__(self):\n"
            "            return (private_root / 'private.txt').read_text(encoding='utf-8')\n"
            "    module.build_runtime_manifest(PrivatePath())"
        ),
    }
    test_file = tmp_path / "test_manifest_failure.py"
    test_file.write_text(
        "from pathlib import Path\n"
        "from utils import runtime_manifest as module\n\n"
        "def test_private_startup_failure(monkeypatch):\n"
        "    private_root = next((Path(__file__).parent / 'assets').iterdir())\n"
        "    " + calls[failure] + "\n", encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH", "")]))
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(config), str(test_file), "-q"],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "ValueError: Runtime manifest" in output
    assert "SYNTHETIC_PATHSECRET" not in output
    assert str(private_root) not in output
    assert SECRET not in output
