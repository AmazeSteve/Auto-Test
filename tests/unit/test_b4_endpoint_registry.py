"""Offline contracts for the source-aware process endpoint registry."""

from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import traceback

import pytest
import requests
import wizbank_api_test
import yaml

from wizbank_api_test.core.endpoint_registry import EndpointRegistry
from wizbank_api_test.utils.yaml_util import YamlUtil


@pytest.fixture(autouse=True)
def isolated_registry_and_no_http(monkeypatch):
    previous = EndpointRegistry._instance
    EndpointRegistry._instance = None

    def reject_http(*args, **kwargs):
        raise AssertionError("B4 registry tests must remain offline")

    monkeypatch.setattr(requests.Session, "request", reject_http)
    try:
        yield
    finally:
        EndpointRegistry._instance = previous


def _write(directory, filename="endpoints.yaml", records=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if records is None:
        records = [{"id": "EP_A", "method": "GET", "path": "/a"}]
    path.write_text(yaml.safe_dump(records, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_no_argument_requires_an_active_valid_snapshot():
    with pytest.raises(RuntimeError, match="initialized"):
        EndpointRegistry()


def test_failed_first_load_does_not_establish_an_active_snapshot(tmp_path):
    with pytest.raises(FileNotFoundError):
        EndpointRegistry(tmp_path / "missing")
    with pytest.raises(RuntimeError, match="initialized"):
        EndpointRegistry()


def test_existing_active_registry_remains_accessible_without_arguments(tmp_path):
    _write(tmp_path)
    registry = EndpointRegistry(tmp_path)
    assert EndpointRegistry() is registry
    assert registry.source_dir == tmp_path.resolve()
    assert registry.list_ids() == ["EP_A"]


def test_same_canonical_source_reuses_snapshot_without_io(tmp_path, monkeypatch):
    source = tmp_path / "端点 fixtures"
    _write(source)
    registry = EndpointRegistry(source)
    monkeypatch.chdir(tmp_path)

    def unexpected_read(*args, **kwargs):
        raise AssertionError("same-source construction must reuse the valid snapshot")

    monkeypatch.setattr(YamlUtil, "read_as_list", unexpected_read)
    assert EndpointRegistry(Path("端点 fixtures") / ".") is registry
    assert registry.source_dir == source.resolve()


def test_different_source_switches_a_to_b_to_a(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    _write(a, records=[{"id": "A", "method": "GET", "path": "/a"}])
    _write(b, records=[{"id": "B", "method": "POST", "path": "/b"}])
    registry = EndpointRegistry(a)
    assert registry.list_ids() == ["A"]
    assert EndpointRegistry(b) is registry
    assert registry.list_ids() == ["B"]
    assert registry.source_dir == b.resolve()
    _write(a, records=[{"id": "A_NEW", "method": "GET", "path": "/a-new"}])
    assert EndpointRegistry(a) is registry
    assert registry.list_ids() == ["A_NEW"]
    assert registry.source_dir == a.resolve()


def test_explicit_load_of_same_source_reloads(tmp_path):
    _write(tmp_path)
    registry = EndpointRegistry(tmp_path)
    _write(tmp_path, records=[{"id": "NEW", "method": "GET", "path": "/new"}])
    assert EndpointRegistry(tmp_path).list_ids() == ["EP_A"]
    registry.load(tmp_path)
    assert registry.list_ids() == ["NEW"]
    assert registry.source_dir == tmp_path.resolve()


def test_empty_directory_is_a_known_valid_empty_snapshot(tmp_path):
    registry = EndpointRegistry(tmp_path)
    assert registry.list_ids() == []
    assert registry.source_dir == tmp_path.resolve()
    assert EndpointRegistry() is registry
    assert not registry.exists("absent")
    with pytest.raises(ValueError, match="不存在"):
        registry.get("absent")


def test_source_metadata_is_read_only(tmp_path):
    _write(tmp_path)
    registry = EndpointRegistry(tmp_path)
    with pytest.raises(AttributeError):
        registry.source_dir = tmp_path / "other"
    assert registry.source_dir == tmp_path.resolve()


def test_endpoint_nested_mutation_is_isolated(tmp_path):
    original = {
        "id": "NESTED", "method": "POST", "path": "/nested", "auth": "admin",
        "host": "Host-be", "headers": {"X-Flags": ["a", {"nested": "initial"}]},
        "tags": ["first"],
    }
    yaml_file = _write(tmp_path, records=[original])
    registry = EndpointRegistry(tmp_path)
    expected = deepcopy(original)
    expected["_file"] = str(yaml_file)
    returned = registry.get("NESTED")
    assert returned == expected
    returned["headers"]["X-Flags"][1]["nested"] = "changed"
    returned["headers"]["new"] = "changed"
    returned["tags"].append("changed")
    returned["method"] = "DELETE"
    assert registry.get("NESTED") == expected


def test_list_ids_result_cannot_mutate_registry(tmp_path):
    _write(tmp_path)
    registry = EndpointRegistry(tmp_path)
    ids = registry.list_ids()
    ids.clear()
    assert registry.list_ids() == ["EP_A"]
    assert registry.exists("EP_A")


def test_traversal_sorts_logical_relative_paths_even_if_scandir_is_reversed(tmp_path, monkeypatch):
    names = ["z.yaml", "nested/01.yaml", "a.yaml", "nested/00.yaml"]
    for index, name in enumerate(names):
        _write(tmp_path, name, [{"id": str(index), "method": "GET", "path": "/"}])
    real_scandir = os.scandir

    @contextmanager
    def reversed_scandir(path):
        with real_scandir(path) as entries:
            yield iter(sorted(entries, key=lambda entry: entry.name, reverse=True))

    monkeypatch.setattr(os, "scandir", reversed_scandir)
    registry = EndpointRegistry(tmp_path)
    assert registry.list_ids() == ["2", "3", "1", "0"]


@pytest.mark.parametrize("via_constructor", [False, True])
@pytest.mark.parametrize("failure", ["missing", "file", "invalid_yaml", "mapping", "scalar", "null", "row", "missing_id", "duplicate"])
def test_failed_source_change_preserves_previous_snapshot_and_source(tmp_path, failure, via_constructor):
    a, b = tmp_path / "A", tmp_path / "B"
    _write(a)
    registry = EndpointRegistry(a)
    previous = registry.get("EP_A")
    secret = "FAKE_B4_PRIVATE_ENDPOINT_VALUE"
    if failure == "file":
        b.write_text("not a directory", encoding="utf-8")
    elif failure != "missing":
        b.mkdir()
        payload = {
            "invalid_yaml": "- id: [\n  password: " + secret,
            "mapping": "password: " + secret,
            "scalar": secret,
            "null": "",
            "row": "- " + secret,
            "missing_id": "- password: " + secret,
            "duplicate": "- id: " + secret + "\n- id: " + secret,
        }[failure]
        (b / "z.yaml").write_text(payload, encoding="utf-8")
        _write(b, "a.yaml", [{"id": "PARTIAL", "method": "GET", "path": "/partial"}])
    with pytest.raises((ValueError, TypeError, OSError)) as caught:
        EndpointRegistry(b) if via_constructor else registry.load(b)
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert EndpointRegistry() is registry
    assert registry.source_dir == a.resolve()
    assert registry.list_ids() == ["EP_A"]
    assert registry.get("EP_A") == previous
    assert not registry.exists("PARTIAL")


@pytest.mark.parametrize("failure", ["scan", "read"])
def test_filesystem_failure_preserves_valid_snapshot(tmp_path, monkeypatch, failure):
    a, b = tmp_path / "A", tmp_path / "B"
    _write(a)
    _write(b)
    registry = EndpointRegistry(a)

    def denied(*args, **kwargs):
        raise PermissionError("FAKE_B4_PRIVATE_FILESYSTEM_DETAIL")

    if failure == "scan":
        monkeypatch.setattr(os, "scandir", denied)
    else:
        monkeypatch.setattr(YamlUtil, "read_as_list", denied)
    with pytest.raises(OSError) as caught:
        registry.load(b)
    assert "FAKE_B4_PRIVATE_FILESYSTEM_DETAIL" not in "".join(traceback.format_exception(caught.value))
    assert registry.source_dir == a.resolve()
    assert registry.list_ids() == ["EP_A"]


def test_failed_same_source_reload_preserves_old_snapshot(tmp_path):
    _write(tmp_path)
    registry = EndpointRegistry(tmp_path)
    _write(tmp_path, records=[{"path": "/missing-id"}])
    with pytest.raises(ValueError):
        registry.load(tmp_path)
    assert EndpointRegistry(tmp_path) is registry
    assert registry.list_ids() == ["EP_A"]
    assert registry.source_dir == tmp_path.resolve()


def test_unreadable_nested_directory_cannot_publish_a_partial_snapshot(tmp_path, monkeypatch):
    a, b = tmp_path / "A", tmp_path / "B"
    _write(a)
    _write(b, "visible.yaml", [{"id": "PARTIAL", "method": "GET", "path": "/visible"}])
    _write(b, "blocked/hidden.yaml", [{"id": "HIDDEN", "method": "GET", "path": "/hidden"}])
    registry = EndpointRegistry(a)
    real_scandir = os.scandir

    def guarded_scandir(path):
        if Path(path) == b / "blocked":
            raise PermissionError("FAKE_B4_PRIVATE_SCAN_DETAIL")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    with pytest.raises(OSError) as caught:
        registry.load(b)
    assert "FAKE_B4_PRIVATE_SCAN_DETAIL" not in "".join(traceback.format_exception(caught.value))
    assert EndpointRegistry() is registry
    assert registry.source_dir == a.resolve()
    assert registry.list_ids() == ["EP_A"]
    assert not registry.exists("PARTIAL")


@pytest.mark.parametrize("via_constructor", [False, True], ids=["load", "constructor"])
@pytest.mark.parametrize("failure", ["malformed", "missing_id", "duplicate", "io", "missing_dir"])
def test_nested_pytest_startup_failure_does_not_render_private_paths_or_yaml(
    tmp_path, failure, via_constructor,
):
    secret_path = "B4_CANARY_PRIVATE_SOURCE_62ef7"
    secret_body = "B4_CANARY_PRIVATE_YAML_37e9b"
    assets = tmp_path / "assets"
    private_source = assets / secret_path
    assets.mkdir()
    if failure != "missing_dir":
        private_source.mkdir()
    payloads = {
        "malformed": "- id: [\n  password: " + secret_body,
        "missing_id": "- password: " + secret_body,
        "duplicate": "- id: " + secret_body + "\n- id: " + secret_body,
        "io": "- id: A\n  path: /a",
    }
    if failure != "missing_dir":
        (private_source / "endpoints.yaml").write_text(payloads[failure], encoding="utf-8")
    else:
        # A directory entry selects the private missing child without embedding it in source.
        (assets / "selected").write_text(secret_path, encoding="utf-8")
    (tmp_path / "valid").mkdir()
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    preamble = (
        "from pathlib import Path\n"
        "from wizbank_api_test.core.endpoint_registry import EndpointRegistry\n"
    )
    if failure == "missing_dir":
        source_expression = "(Path(__file__).parent / 'assets' / (Path(__file__).parent / 'assets' / 'selected').read_text(encoding='utf-8'))"
    else:
        source_expression = "next((Path(__file__).parent / 'assets').iterdir())"
    operation = "EndpointRegistry(" + source_expression + ")"
    if not via_constructor:
        operation = "EndpointRegistry(Path(__file__).parent / 'valid').load(" + source_expression + ")"
    test_file = tmp_path / "test_startup.py"
    test_file.write_text(preamble + "\ndef test_startup():\n    " + operation + "\n", encoding="utf-8")
    conftest = (
        "import os\n"
        "import pytest\n"
        "import requests\n"
        "from wizbank_api_test.utils.yaml_util import YamlUtil\n"
        "@pytest.fixture(autouse=True)\n"
        "def offline(monkeypatch):\n"
        "    def reject(*args, **kwargs):\n"
        "        raise AssertionError('B4 nested registry tests must remain offline')\n"
        "    monkeypatch.setattr(requests.Session, 'request', reject)\n"
    )
    if failure == "io":
        conftest += (
            "    def denied(*args, **kwargs):\n"
            "        raise PermissionError(os.environ['B4_TEST_PRIVATE_DETAIL'])\n"
            "    monkeypatch.setattr(YamlUtil, 'read_as_list', denied)\n"
        )
    (tmp_path / "conftest.py").write_text(conftest, encoding="utf-8")
    env = os.environ.copy()
    package_parent = Path(next(iter(wizbank_api_test.__path__))).parent
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(package_parent), str(Path(__file__).resolve().parents[3]), env.get("PYTHONPATH", ""),
    ]))
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTEST_ADDOPTS"] = ""
    env["B4_TEST_PRIVATE_DETAIL"] = secret_body
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(config), str(test_file), "-q"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "1 failed" in output
    assert secret_path not in output
    assert secret_body not in output
    assert "ModuleNotFoundError" not in output
    expected_error = {"io": "OSError", "missing_dir": "FileNotFoundError"}.get(failure, "ValueError")
    assert expected_error + ":" in output
