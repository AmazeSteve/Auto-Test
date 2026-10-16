"""Private file bytes/paths stay outside B5's diagnostic surfaces."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import allure
import pytest
import requests
import wizbank_api_test

from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.client import ApiClient
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.file_resolver import FileResolutionError, FileResolver


PRIVATE_PATH = "PRIVATE_ABSOLUTE_PATH_SENTINEL"
PRIVATE_BYTES = b"PRIVATE_FILE_CONTENT_SENTINEL"
PRIVATE_REFERENCE = "private_fixture_reference"
BINARY_CONTENT = b"\x00\xff\r\n" + PRIVATE_BYTES + "中文".encode("utf-8")


@pytest.fixture(autouse=True)
def offline(monkeypatch, bind_context):
    def reject_network(*args, **kwargs):
        raise AssertionError("B5 security tests forbid real network")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(requests.Session, "send", reject_network)
    Context.clear()
    yield
    Context.clear()


@pytest.fixture
def private_fixture(tmp_path, monkeypatch):
    root = tmp_path / PRIVATE_PATH / "fixtures"
    directory = root / PRIVATE_REFERENCE
    directory.mkdir(parents=True)
    fixture = directory / "asset.bin"
    fixture.write_bytes(BINARY_CONTENT)
    monkeypatch.setattr(FileResolver, "_fixture_root", root)
    spec = {"upload": {
        "fixture": f"{PRIVATE_REFERENCE}/asset.bin",
        "filename": "public-name.bin",
        "content_type": "application/x-offline-example",
    }}
    return root, fixture, spec


def assert_no_private_evidence(value, root=None):
    evidence = str(value)
    assert PRIVATE_PATH not in evidence
    assert PRIVATE_BYTES.decode("ascii") not in evidence
    assert PRIVATE_REFERENCE not in evidence
    if root is not None:
        assert str(root) not in evidence


def response_for(request):
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"code":200}'
    response.headers["Content-Type"] = "application/json"
    response.request = request
    return response


def test_resolver_and_borrowed_handle_representations_hide_private_paths(private_fixture):
    root, _, spec = private_fixture
    scope = FileResolver.resolve(spec)
    assert_no_private_evidence(repr(scope), root)
    with scope as resolved:
        handle = resolved.request_files["upload"][1]
        assert_no_private_evidence(repr(handle), root)
        assert_no_private_evidence(repr(resolved.request_files), root)
        assert_no_private_evidence(repr(resolved), root)
        assert handle.read() == BINARY_CONTENT
    assert handle.closed
    assert_no_private_evidence(repr(handle), root)


@pytest.mark.parametrize("explicit_mime", [True, False])
def test_file_evidence_contains_only_allowed_public_metadata(private_fixture, explicit_mime):
    root, _, spec = private_fixture
    if not explicit_mime:
        spec["upload"].pop("content_type")
    with FileResolver.resolve(spec) as resolved:
        assert resolved.evidence == [{
            "field": "upload",
            "filename": "public-name.bin",
            "size": len(BINARY_CONTENT),
            "content_type": "application/x-offline-example" if explicit_mime else None,
        }]
        assert set(resolved.evidence[0]) == {"field", "filename", "size", "content_type"}
        assert_no_private_evidence(json.dumps(resolved.evidence), root)


@pytest.mark.parametrize("kind", ["permission", "oserror", "missing"])
def test_filesystem_error_strings_and_reprs_do_not_expose_private_input(private_fixture, monkeypatch, kind):
    root, fixture, spec = private_fixture

    def denied(path):
        error_type = PermissionError if kind == "permission" else OSError
        raise error_type(f"{path}: {PRIVATE_BYTES.decode('ascii')}")

    if kind == "missing":
        fixture.unlink()
    else:
        monkeypatch.setattr(FileResolver, "_open_binary", staticmethod(denied))
    with pytest.raises(FileResolutionError) as raised:
        with FileResolver.resolve(spec):
            pytest.fail("Invalid fixture was opened")
    assert_no_private_evidence(str(raised.value), root)
    assert_no_private_evidence(repr(raised.value), root)
    assert raised.value.__suppress_context__


def test_read_error_hides_raw_oserror_and_closes_handle(private_fixture, monkeypatch):
    root, fixture, spec = private_fixture
    raw = fixture.open("rb")

    class BrokenRead:
        @property
        def closed(self):
            return raw.closed

        def fileno(self):
            return raw.fileno()

        def read(self, size=-1):
            raise OSError(f"{fixture}: {PRIVATE_BYTES.decode('ascii')}")

        def close(self):
            raw.close()

    monkeypatch.setattr(FileResolver, "_open_binary", staticmethod(lambda path: BrokenRead()))
    with pytest.raises(FileResolutionError) as raised:
        with FileResolver.resolve(spec) as resolved:
            resolved.request_files["upload"][1].read()
    assert_no_private_evidence(str(raised.value), root)
    assert raw.closed


def test_real_requests_encoding_uses_bytes_but_allure_attachments_use_only_safe_file_metadata(
    private_fixture, monkeypatch,
):
    root, _, spec = private_fixture
    attachments = []
    prepared = []
    monkeypatch.setattr(allure, "attach", lambda body, **kwargs: attachments.append((body, kwargs)))
    client = ApiClient("https://example.invalid", principal="none", host_key="Host-be")

    def send(request, **kwargs):
        prepared.append(request)
        return response_for(request)

    monkeypatch.setattr(client.session, "send", send)
    case = {
        "id": "OFFLINE_FILE_EVIDENCE", "method": "POST", "path": "/offline-multipart",
        "auth": "none", "files": spec,
        "validate": [{"type": "status_code", "expected": 200}],
    }
    try:
        CaseRunner.run(case, lambda host: client, None, apply_metadata=False)
    finally:
        client.session.close()
    assert len(prepared) == 1
    assert BINARY_CONTENT in prepared[0].body
    assert "multipart/form-data; boundary=" in prepared[0].headers["Content-Type"]
    file_attachments = [body for body, metadata in attachments if metadata.get("name") == "multipart files"]
    assert len(file_attachments) == 1
    assert json.loads(file_attachments[0]) == [{
        "field": "upload", "filename": "public-name.bin", "size": len(BINARY_CONTENT),
        "content_type": "application/x-offline-example",
    }]
    assert_no_private_evidence(attachments, root)


@pytest.mark.parametrize("failure,entrypoint", [
    (failure, entrypoint)
    for entrypoint in ("resolver", "runner")
    for failure in ("malformed", "missing", "permission", "read", "transport")
    if entrypoint == "runner" or failure != "transport"
])
def test_default_nested_pytest_failure_output_hides_fixture_paths_and_contents(
    private_fixture, tmp_path, failure, entrypoint,
):
    root, fixture, spec = private_fixture
    unrelated = tmp_path / "nested pytest unrelated cwd"
    unrelated.mkdir()
    pytest_config = unrelated / "pytest.ini"
    pytest_config.write_text("[pytest]\n", encoding="utf-8")
    test_file = unrelated / "test_private_file_failure.py"
    test_file.write_text(
        "import json, os, socket\n"
        "from pathlib import Path\n"
        "import pytest, requests\n"
        "from wizbank_api_test.core.case_runner import CaseRunner\n"
        "from wizbank_api_test.core.client import ApiClient\n"
        "from wizbank_api_test.core.file_resolver import FileResolver\n"
        "def test_private_file_failure(monkeypatch):\n"
        "    def reject_network(*args, **kwargs):\n"
        "        raise AssertionError('real socket is forbidden')\n"
        "    monkeypatch.setattr(socket.socket, 'connect', reject_network)\n"
        "    monkeypatch.setattr(socket.socket, 'connect_ex', reject_network)\n"
        "    monkeypatch.setattr(socket, 'create_connection', reject_network)\n"
        "    monkeypatch.setattr(FileResolver, '_fixture_root', Path(os.environ['B5_PRIVATE_ROOT']))\n"
        "    mode = os.environ['B5_FAILURE_KIND']\n"
        "    spec = json.loads(os.environ['B5_FILES_SPEC'])\n"
        "    if mode == 'malformed':\n"
        "        spec['upload']['fixture'] = os.environ['B5_PRIVATE_FILE']\n"
        "    if mode == 'permission':\n"
        "        def denied(path):\n"
        "            raise PermissionError(os.environ['B5_PRIVATE_FILE'] + os.environ['B5_PRIVATE_BYTES'])\n"
        "        monkeypatch.setattr(FileResolver, '_open_binary', staticmethod(denied))\n"
        "    if mode == 'read':\n"
        "        raw = Path(os.environ['B5_PRIVATE_FILE']).open('rb')\n"
        "        class BrokenRead:\n"
        "            @property\n"
        "            def closed(self): return raw.closed\n"
        "            def fileno(self): return raw.fileno()\n"
        "            def close(self): raw.close()\n"
        "            def read(self, size=-1):\n"
        "                raise OSError(os.environ['B5_PRIVATE_FILE'] + os.environ['B5_PRIVATE_BYTES'])\n"
        "        monkeypatch.setattr(FileResolver, '_open_binary', staticmethod(lambda path: BrokenRead()))\n"
        "    if os.environ['B5_ENTRYPOINT'] == 'resolver':\n"
        "        with FileResolver.resolve(spec) as resolved:\n"
        "            resolved.request_files['upload'][1].read()\n"
        "        return\n"
        "    client = ApiClient('https://example.invalid', principal='none', host_key='Host-be')\n"
        "    def send(request, **kwargs):\n"
        "        raise RuntimeError('offline transport failure')\n"
        "    monkeypatch.setattr(client.session, 'send', send)\n"
        "    case = {'id':'SAFE_OUTPUT', 'method':'POST', 'path':'/offline', 'files':spec, 'auth':'none'}\n"
        "    CaseRunner.run(case, lambda host: client, None, apply_metadata=False)\n",
        encoding="utf-8",
    )
    if failure == "missing":
        fixture.unlink()
    env = os.environ.copy()
    package_parent = Path(next(iter(wizbank_api_test.__path__))).parent
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(package_parent), env.get("PYTHONPATH", "")]))
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["B5_PRIVATE_ROOT"] = str(root)
    env["B5_PRIVATE_FILE"] = str(fixture)
    env["B5_PRIVATE_BYTES"] = PRIVATE_BYTES.decode("ascii")
    env["B5_FILES_SPEC"] = json.dumps(spec)
    env["B5_FAILURE_KIND"] = failure
    env["B5_ENTRYPOINT"] = entrypoint
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(pytest_config), str(test_file), "-q"],
        cwd=unrelated, env=env, capture_output=True, text=True, timeout=60,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "1 failed" in output
    expected_error = "offline transport failure" if failure == "transport" else "FileResolutionError"
    assert expected_error in output
    assert_no_private_evidence(output, root)
