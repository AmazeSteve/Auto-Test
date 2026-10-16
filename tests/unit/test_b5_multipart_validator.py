"""Static B5 multipart contracts; no fixture files or transport are required."""

from collections import UserDict
from copy import deepcopy
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys

import pytest
import requests
import wizbank_api_test

from wizbank_api_test.core.case_validator import CaseValidator
from wizbank_api_test.core.endpoint_registry import EndpointRegistry


def case(**body):
    return {
        "id": "B5_STATIC", "name": "Offline multipart contract",
        "method": "POST", "path": "/offline-multipart", **body,
    }


def files():
    return {"upload": {"fixture": "course/demo.pdf"}}


@pytest.fixture(autouse=True)
def offline_registry(monkeypatch, tmp_path):
    endpoint_dir = tmp_path / "endpoints"
    endpoint_dir.mkdir()
    monkeypatch.setattr(EndpointRegistry, "_instance", None)
    EndpointRegistry(endpoint_dir)

    def reject_transport(*args, **kwargs):
        raise AssertionError("network is forbidden in multipart validation")

    monkeypatch.setattr(requests.Session, "request", reject_transport)
    monkeypatch.setattr(requests.Session, "send", reject_transport)


def test_legacy_raw_files_are_not_a_valid_static_contract():
    with pytest.raises(ValueError, match="files"):
        CaseValidator.validate_all([case(files={"upload": "course/demo.pdf"})])


def test_data_plus_files_is_a_valid_body_state():
    CaseValidator.validate_all([case(data={"title": "{{title}}"}, files=files())])


@pytest.mark.parametrize("body", [
    {},
    {"json": {"value": "{{value}}"}},
    {"data": "legacy raw form data"},
    {"files": {"upload": {"fixture": "course/demo.pdf"}}},
    {"files": {"cover": {"fixture": "course/cover.png"},
               "attachment": {"fixture": "course/demo.pdf"}}},
    {"data": {"title": "{{title}}", "counts": [1, 2.5, True, "three"]},
     "files": {"upload": {"fixture": "course/中文 文件.pdf",
                          "filename": "report.pdf", "content_type": "application/pdf"}}},
], ids=["no-body", "json", "data-only", "file-only", "different-fields", "form-and-file"])
def test_legal_static_body_states(body):
    original = deepcopy(body)
    CaseValidator.validate_all([case(**body)])
    assert body == original


@pytest.mark.parametrize("body", [
    {"json": {}, "data": {"title": "x"}},
    {"json": {}, "files": {"upload": {"fixture": "demo.pdf"}}},
    {"json": {}, "data": {"title": "x"}, "files": {"upload": {"fixture": "demo.pdf"}}},
    {"request_builder": {}, "files": {}},
    {"request_builder": {}, "data": {}},
    {"restore_payload": {}, "files": {}},
    {"restore_payload": {}, "data": {}},
    {"request_builder": {}, "restore_payload": {}},
], ids=["json-data", "json-files", "all-raw", "builder-files", "builder-data",
        "restore-files", "restore-data", "two-producers"])
def test_other_body_combinations_remain_invalid(body):
    assert CaseValidator._validate_body_conflict([case(**body)])


@pytest.mark.parametrize("json_body", [None, {}], ids=["none-json", "empty-json"])
@pytest.mark.parametrize("other_body", [
    {"data": None}, {"data": {}}, {"files": None}, {"files": {}},
], ids=["none-data", "empty-data", "none-files", "empty-files"])
def test_json_producer_cannot_coexist_with_empty_or_none_data_or_files(json_body, other_body):
    candidate = case(json=json_body, **other_body)
    assert CaseValidator._validate_body_conflict([candidate])
    with pytest.raises(ValueError, match="不允许同时配置"):
        CaseValidator.validate_all([candidate])


@pytest.mark.parametrize("data", [None, {}, {"title": "x"}], ids=["absent", "empty", "form"])
def test_get_does_not_accept_files(data):
    body = {"files": files()}
    if data is not None:
        body["data"] = data
    candidate = case(**body)
    candidate["method"] = "GET"
    with pytest.raises(ValueError, match="GET.*files"):
        CaseValidator.validate_all([candidate])


@pytest.mark.parametrize("spec", [
    None, "course/demo.pdf", [], ("demo.pdf", BytesIO(b"data")),
    {"": {"fixture": "demo.pdf"}},
    {"{{field}}": {"fixture": "demo.pdf"}},
    {"upload": "course/demo.pdf"},
    {"upload": ("demo.pdf", BytesIO(b"data"))},
    {"upload": [{"fixture": "a.pdf"}, {"fixture": "b.pdf"}]},
    {"upload": {}},
    {"upload": {"fixture": "demo.pdf", "headers": {}}},
    {"upload": {"fixture": "{{fixture}}"}},
    {"upload": {"fixture": "$data.file"}},
    {"upload": {"fixture": "$timestamp_ms"}},
    {"upload": {"fixture": "demo.pdf", "filename": ""}},
    {"upload": {"fixture": "demo.pdf", "filename": "dir/demo.pdf"}},
    {"upload": {"fixture": "demo.pdf", "filename": "{{filename}}.pdf"}},
    {"upload": {"fixture": "demo.pdf", "content_type": ""}},
    {"upload": {"fixture": "demo.pdf", "content_type": "text/plain\r\nX: y"}},
    {"upload": {"fixture": "demo.pdf", "content_type": "{{$data.mime}}"}},
], ids=["none", "string", "list", "tuple", "empty-field", "dynamic-field",
        "shorthand", "raw-file-tuple", "same-field-multiple", "missing-fixture", "unknown-key",
        "dynamic-fixture", "data-fixture", "timestamp-fixture", "empty-filename",
        "path-filename", "dynamic-filename", "empty-mime", "mime-injection", "dynamic-mime"])
def test_invalid_files_schema_is_rejected(spec):
    with pytest.raises(ValueError, match="files"):
        CaseValidator.validate_all([case(files=spec)])


def test_mapping_contract_does_not_require_concrete_dict():
    CaseValidator.validate_all([case(files=UserDict({
        "upload": UserDict({"fixture": "demo.pdf"}),
    }), data=UserDict({"title": "x"}))])


@pytest.mark.parametrize("data", [
    "raw form", None, b"bytes", {"value": None}, {"value": {}},
    {"value": [["nested"]]}, {"value": b"bytes"}, {"value": (1, 2)},
    {"value": {1, 2}}, {"value": BytesIO(b"data")}, {"": "empty-key"},
    {1: "numeric-key"}, {"{{dynamic_key}}": "value"},
], ids=["string", "none", "bytes", "none-value", "nested-dict", "nested-list", "bytes-value",
        "tuple-value", "set-value", "file-value", "empty-key", "numeric-key", "dynamic-key"])
def test_invalid_multipart_data_shape_is_rejected(data):
    with pytest.raises(ValueError, match="multipart|data"):
        CaseValidator.validate_all([case(data=data, files=files())])


def test_generator_and_arbitrary_iterable_are_not_multipart_data():
    class Iterable:
        def __iter__(self):
            yield "value"

    for data in ((value for value in ["x"]), Iterable(), {"value": (value for value in ["x"])},
                 {"value": Iterable()}):
        with pytest.raises(ValueError, match="multipart|data"):
            CaseValidator.validate_all([case(data=data, files=files())])


@pytest.mark.parametrize("data", ["raw body", None, {"value": None}, {"nested": {"value": "x"}}])
def test_empty_files_does_not_tighten_data_only_shape(data):
    CaseValidator.validate_all([case(data=data, files={})])


@pytest.mark.parametrize("header", ["Content-Type", "content-type", "CONTENT-TYPE", "CoNtEnT-TyPe"])
@pytest.mark.parametrize("value", [None, "", "application/pdf", "multipart/form-data; boundary=manual"])
def test_static_content_type_key_presence_is_forbidden_with_nonempty_files(header, value):
    with pytest.raises(ValueError, match="Content-Type"):
        CaseValidator.validate_all([case(files=files(), headers={header: value})])


def test_multipart_content_type_guard_never_interprets_the_header_value():
    class PrivateValue:
        def __str__(self):
            raise AssertionError("header value must not be inspected")

    with pytest.raises(ValueError, match="Content-Type"):
        CaseValidator.validate_all([case(files=files(), headers=UserDict({
            "Content-Type": PrivateValue(),
        }))])


def test_empty_files_keeps_existing_content_type_data_semantics():
    CaseValidator.validate_all([case(data={"title": "x"}, files={},
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})])


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("file_value", [None, {}, {"upload": {"fixture": "demo.pdf"}}],
                         ids=["none", "empty", "descriptor"])
@pytest.mark.parametrize("enabled", [True, False])
def test_effect_capture_files_is_rejected_even_without_endpoint_or_when_disabled(phase, file_value, enabled):
    candidate = case(effect={"capture": {"enabled": enabled, phase: {"files": file_value}}})
    with pytest.raises(ValueError, match=rf"effect\.capture\.{phase}.*files"):
        CaseValidator.validate_all([candidate])


def test_files_schema_static_validation_does_not_access_the_filesystem(monkeypatch):
    from pathlib import Path

    def reject_filesystem(*args, **kwargs):
        raise AssertionError("static validation must not inspect fixture files")

    monkeypatch.setattr(Path, "resolve", reject_filesystem)
    monkeypatch.setattr(Path, "open", reject_filesystem)
    monkeypatch.setattr(Path, "stat", reject_filesystem)
    CaseValidator.validate_all([case(files=files())])


def test_invalid_files_do_not_enter_data_factory_validation_or_leak_raw_paths():
    sentinel = "PRIVATE_ABSOLUTE_PATH_SENTINEL"
    descriptor = {"fixture": "/" + sentinel + "/{{$data.PRIVATE_FACTORY_SENTINEL}}"}
    candidate = case(files={"upload": descriptor})
    assert CaseValidator._validate_data_factory_references([candidate]) == []
    with pytest.raises(ValueError) as caught:
        CaseValidator.validate_all([candidate])
    assert sentinel not in str(caught.value)
    assert "PRIVATE_FACTORY_SENTINEL" not in str(caught.value)


@pytest.mark.parametrize("phase", ["before", "after"])
def test_effect_files_are_rejected_without_interpreting_dynamic_references(phase):
    candidate = case(effect={"capture": {
        "enabled": False, phase: {"files": {
            "upload": {"fixture": "{{$data.PRIVATE_FACTORY_SENTINEL}}"},
        }},
    }})
    assert CaseValidator._validate_data_factory_references([candidate]) == []
    with pytest.raises(ValueError) as caught:
        CaseValidator.validate_all([candidate])
    assert "PRIVATE_FACTORY_SENTINEL" not in str(caught.value)
    assert f"effect.capture.{phase}" in str(caught.value)


def test_business_json_field_named_files_still_checks_data_factory_references():
    errors = CaseValidator._validate_data_factory_references([
        case(json={"files": "{{$data.this_key_does_not_exist}}"}),
    ])
    assert len(errors) == 1
    assert "this_key_does_not_exist" in errors[0]


def test_default_pytest_validation_failure_does_not_print_private_fixture_input(tmp_path):
    child = tmp_path / "nested validator 安全"
    child.mkdir()
    probe = child / "test_private_input.py"
    probe.write_text(
        "import os\n"
        "from wizbank_api_test.core.case_validator import CaseValidator\n"
        "from wizbank_api_test.core.endpoint_registry import EndpointRegistry\n"
        "def test_invalid_fixture(tmp_path):\n"
        "    directory = tmp_path / 'endpoints'\n"
        "    directory.mkdir()\n"
        "    EndpointRegistry(directory)\n"
        "    candidate = {'id': 'B5_STATIC', 'name': 'Offline', 'method': 'POST', "
        "'path': '/offline', 'files': {'upload': {'fixture': "
        "os.environ['B5_PRIVATE_INPUT']}}}\n"
        "    CaseValidator.validate_all([candidate])\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["B5_PRIVATE_INPUT"] = "/PRIVATE_ABSOLUTE_PATH_SENTINEL/private.pdf"
    package_parent = Path(next(iter(wizbank_api_test.__path__))).parent
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(package_parent), env.get("PYTHONPATH", "")]))
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q"],
        cwd=child, env=env, capture_output=True, text=True, timeout=60,
    )
    evidence = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "Multipart fixture reference is unsafe" in evidence
    assert "1 failed" in evidence
    assert "PRIVATE_ABSOLUTE_PATH_SENTINEL" not in evidence
    assert "PRIVATE_FILE_CONTENT_SENTINEL" not in evidence
