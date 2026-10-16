"""B5 uses real Requests preparation with fake, socket-free transport."""

from copy import deepcopy
from email import policy
from email.parser import BytesParser
import io
import socket

import allure
import pytest
import requests

from wizbank_api_test.core.assertions import Assertor
from wizbank_api_test.core.case_runner import CaseRunner
from wizbank_api_test.core.client import ApiClient
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.effect_executor import EffectExecutor
from wizbank_api_test.core.extractor import Extractor
from wizbank_api_test.core.file_resolver import FileResolutionError, FileResolver
from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.utils.test_data_factory import TestDataFactory as _TestDataFactory


BINARY = b"\x00\xff\r\n" + "测试 binary".encode("utf-8")


class _TrackedFile:
    def __init__(self, handle):
        self._handle = handle
        self.close_count = 0

    @property
    def closed(self):
        return self._handle.closed

    def fileno(self):
        return self._handle.fileno()

    def read(self, *args):
        return self._handle.read(*args)

    def close(self):
        self.close_count += 1
        return self._handle.close()

    def __repr__(self):
        return "<TrackedBinaryFile>"


@pytest.fixture(autouse=True)
def offline(monkeypatch, bind_context):
    Context.clear()

    def reject(*args, **kwargs):
        raise AssertionError("Real network is forbidden in B5 tests")

    monkeypatch.setattr(requests.Session, "send", reject)
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(allure, "attach", lambda *args, **kwargs: None)
    yield
    Context.clear()


@pytest.fixture
def fixtures(tmp_path, monkeypatch):
    root = tmp_path / "fixture root"
    root.mkdir()
    (root / "binary.bin").write_bytes(BINARY)
    (root / "second.txt").write_bytes(b"second bytes")
    monkeypatch.setattr(FileResolver, "_fixture_root", root)
    opened = []

    def tracked_open(path):
        handle = _TrackedFile(path.open("rb"))
        opened.append(handle)
        return handle

    monkeypatch.setattr(FileResolver, "_open_binary", staticmethod(tracked_open))
    return root, opened


def _case(**body):
    return {
        "id": "B5_OFFLINE", "method": "POST", "path": "/offline-multipart",
        "auth": "none", **body,
    }


def _files(**overrides):
    return {"upload": {"fixture": "binary.bin", **overrides}}


def _response(prepared):
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"code":200}'
    response.encoding = "utf-8"
    response.request = prepared
    response.url = prepared.url
    return response


def _client(monkeypatch, opened, *, defaults=None, send_error=None):
    client = ApiClient("https://offline.invalid", default_headers=defaults, principal="none")
    sent = []

    def send(prepared, **kwargs):
        assert isinstance(prepared, requests.PreparedRequest)
        assert all(not handle.closed for handle in opened)
        sent.append(prepared)
        if send_error:
            raise send_error
        return _response(prepared)

    monkeypatch.setattr(client.session, "send", send)
    return client, sent


def _run(case, client):
    return CaseRunner.run(case, lambda host: client, None, apply_metadata=False)


def _assert_closed(opened, count=1):
    assert len(opened) == count
    assert all(handle.closed and handle.close_count == 1 for handle in opened)


def _parts(prepared):
    content_type = prepared.headers["Content-Type"]
    assert content_type.startswith("multipart/form-data;")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + prepared.body
    )
    boundary = message.get_boundary()
    assert boundary
    assert prepared.body.startswith(b"--" + boundary.encode("ascii") + b"\r\n")
    assert prepared.body.endswith(b"--" + boundary.encode("ascii") + b"--\r\n")
    return list(message.iter_parts())


@pytest.mark.parametrize("mime", [None, "application/x-b5-binary"])
@pytest.mark.parametrize("filename", [None, "custom binary.bin"])
@pytest.mark.parametrize("with_data", [False, True])
def test_real_preparation_preserves_binary_filename_mime_and_boundary(
    monkeypatch, fixtures, mime, filename, with_data,
):
    _, opened = fixtures
    descriptor = {}
    if mime is not None:
        descriptor["content_type"] = mime
    if filename is not None:
        descriptor["filename"] = filename
    body = {"files": _files(**descriptor)}
    if with_data:
        Context.set_variable("title", "Resolved Title")
        body["data"] = {"title": "{{title}}", "repeat": ["a", 3, False]}
    case = _case(**body)
    original = deepcopy(case)
    evidence = []
    monkeypatch.setattr(allure, "attach", lambda value, **kwargs: evidence.append((kwargs.get("name"), value)))
    client, sent = _client(monkeypatch, opened)

    response = _run(case, client)

    assert response.status_code == 200
    assert len(sent) == 1
    parts = _parts(sent[0])
    file_part = [part for part in parts if part.get_param("name", header="Content-Disposition") == "upload"][0]
    assert file_part.get_filename() == (filename or "binary.bin")
    assert file_part.get_payload(decode=True) == BINARY
    assert file_part.get("Content-Type") == mime
    if with_data:
        ordinary = [part for part in parts if part.get_filename() is None]
        assert [part.get_payload(decode=True) for part in ordinary] == [b"Resolved Title", b"a", b"3", b"False"]
    assert any(name == "multipart files" for name, _ in evidence)
    _assert_closed(opened)
    assert case == original


def test_multiple_different_file_fields_use_real_preparation(monkeypatch, fixtures):
    _, opened = fixtures
    files = {**_files(), "attachment": {"fixture": "second.txt", "content_type": "text/plain"}}
    client, sent = _client(monkeypatch, opened)

    _run(_case(files=files), client)

    parts = _parts(sent[0])
    assert [part.get_param("name", header="Content-Disposition") for part in parts] == ["upload", "attachment"]
    assert [part.get_payload(decode=True) for part in parts] == [BINARY, b"second bytes"]
    _assert_closed(opened, 2)


@pytest.mark.parametrize("second_pass", ["cached-mapping", "factory-mapping", "placeholder", "timestamp"])
def test_resolved_multipart_data_is_final_before_api_client(monkeypatch, fixtures, second_pass):
    """The frozen ApiClient must not reinterpret resolved multipart scalars."""
    _, opened = fixtures
    literal = {
        "cached-mapping": "$data.invalid_shape",
        "factory-mapping": "$data.invalid_shape",
        "placeholder": "{{literal_user_text}}",
        "timestamp": "$timestamp_ms",
    }[second_pass]
    Context.set_variable("first", literal)
    Context.set_variable("title", "Resolved Title")
    if second_pass == "cached-mapping":
        Context.set_variable("data.invalid_shape", {"nested": "value"})
    factory_calls = []

    def factory(key):
        factory_calls.append(key)
        if key == "unique_display_name":
            assert opened == []
            return "Generated Title"
        return {"nested": "value"}

    def unexpected_timestamp():
        raise AssertionError("Resolved multipart data must not be resolved again")

    monkeypatch.setattr(_TestDataFactory, "resolve", factory)
    monkeypatch.setattr(VariableResolver, "timestamp_ms", unexpected_timestamp)
    case = _case(files=_files(), data={
        "title": "{{first}}", "ordinary": "{{title}}", "repeat": ["{{first}}", "{{title}}"],
        "generated": "{{$data.unique_display_name}}",
        "password": "PRIVATE_MULTIPART_FORM_SECRET",
    })
    original = deepcopy(case)
    evidence = []
    monkeypatch.setattr(allure, "attach", lambda value, **kwargs: evidence.append((kwargs.get("name"), str(value))))
    client, sent = _client(monkeypatch, opened)

    _run(case, client)

    ordinary = [part for part in _parts(sent[0]) if part.get_filename() is None]
    assert [part.get_payload(decode=True) for part in ordinary] == [
        literal.encode("utf-8"), b"Resolved Title", literal.encode("utf-8"), b"Resolved Title",
        b"Generated Title", b"PRIVATE_MULTIPART_FORM_SECRET",
    ]
    assert factory_calls == ["unique_display_name"]
    assert case == original
    if second_pass == "cached-mapping":
        assert Context.get_variable("data.invalid_shape") == {"nested": "value"}
    assert all("PRIVATE_MULTIPART_FORM_SECRET" not in body for _, body in evidence)
    _assert_closed(opened)


def test_handles_are_closed_before_response_assertions(monkeypatch, fixtures):
    _, opened = fixtures
    client, sent = _client(monkeypatch, opened)
    observed = []

    def assert_response(response, validations):
        _assert_closed(opened)
        observed.append(response)

    monkeypatch.setattr(Assertor, "run_validations", assert_response)
    response = _run(_case(files=_files()), client)
    assert observed == [response]
    assert len(sent) == 1


@pytest.mark.parametrize("source", ["defaults", "case"])
@pytest.mark.parametrize("key", ["Content-Type", "content-type", "cOnTeNt-TyPe"])
@pytest.mark.parametrize("value", ["multipart/form-data; boundary=manual", "application/json", None, ""])
def test_content_type_presence_rejects_before_open(monkeypatch, fixtures, source, key, value):
    _, opened = fixtures
    headers = {key: value}
    client, sent = _client(monkeypatch, opened, defaults=headers if source == "defaults" else None)
    case = _case(files=_files(), **({"headers": headers} if source == "case" else {}))

    with pytest.raises(FileResolutionError, match="Content-Type"):
        _run(case, client)

    assert opened == sent == []


@pytest.mark.parametrize("failure", ["variable", "header", "before", "builder"])
def test_pre_request_work_fails_before_any_file_open(monkeypatch, fixtures, failure):
    _, opened = fixtures
    case = _case(files=_files())
    client, sent = _client(monkeypatch, opened)
    if failure == "variable":
        case["data"] = {"title": "{{missing_runtime_variable}}"}
    elif failure == "header":
        case["headers"] = {"X-Title": "{{missing_runtime_variable}}"}
    else:
        case["effect"] = _effect()
        monkeypatch.setattr(EffectExecutor, "capture_before", lambda **kwargs: _fail("before failure") if failure == "before" else {})
        if failure == "builder":
            case["request_builder"] = {"invalid": "B5 pre-request failure"}

    with pytest.raises((ValueError, KeyError)):
        _run(case, client)

    assert opened == sent == []
    assert not Context.has_effect(case["id"])


def _effect():
    return {"target": {"fields": ["value"]}, "capture": {"before": {"endpoint": "before"}, "after": {"endpoint": "after"}}}


def _fail(message):
    raise ValueError(message)


@pytest.mark.parametrize("failure", ["client", "prepare", "send", "evidence", "assertion", "extract", "after"])
def test_handles_close_exactly_once_for_every_failure_path(monkeypatch, fixtures, failure):
    _, opened = fixtures
    case = _case(files=_files())
    client, sent = _client(monkeypatch, opened, send_error=ValueError("send failure") if failure == "send" else None)
    if failure == "client":
        monkeypatch.setattr(client, "request", lambda **kwargs: _fail("client failure"))
    elif failure == "prepare":
        monkeypatch.setattr(client.session, "prepare_request", lambda request: _fail("prepare failure"))
    elif failure == "evidence":
        def attach(body, **kwargs):
            if kwargs.get("name") == "multipart files":
                assert len(opened) == 1 and not opened[0].closed
                _fail("evidence failure")
        monkeypatch.setattr(allure, "attach", attach)
    elif failure == "assertion":
        def assertions(*args):
            _assert_closed(opened)
            _fail("assertion failure")
        monkeypatch.setattr(Assertor, "run_validations", assertions)
    elif failure == "extract":
        def extract(*args):
            _assert_closed(opened)
            _fail("extract failure")
        monkeypatch.setattr(Extractor, "run_extractors", extract)
    elif failure == "after":
        case["effect"] = _effect()
        monkeypatch.setattr(EffectExecutor, "capture_before", lambda **kwargs: {"value": "baseline"})
        def after(**kwargs):
            _assert_closed(opened)
            _fail("after failure")
        monkeypatch.setattr(EffectExecutor, "capture_after", after)

    with pytest.raises(ValueError, match=failure + " failure"):
        _run(case, client)

    _assert_closed(opened)
    assert len(sent) == (1 if failure in ("send", "assertion", "extract", "after") else 0)


@pytest.mark.parametrize("failure", ["resolver", "evidence"])
def test_pre_request_file_failures_do_not_mark_effect_unknown(monkeypatch, fixtures, failure):
    _, opened = fixtures
    client, sent = _client(monkeypatch, opened)
    case = _case(files=_files(), effect=_effect())
    monkeypatch.setattr(EffectExecutor, "capture_before", lambda **kwargs: {"value": "baseline"})
    if failure == "resolver":
        case["files"]["upload"]["fixture"] = "missing.bin"
    else:
        def attach(body, **kwargs):
            if kwargs.get("name") == "multipart files":
                _fail("evidence failure")
        monkeypatch.setattr(allure, "attach", attach)

    with pytest.raises(ValueError):
        _run(case, client)

    assert sent == []
    assert not Context.has_effect(case["id"])
    if failure == "evidence":
        _assert_closed(opened)
    else:
        assert opened == []


def test_partial_open_failure_closes_first_handle_and_sends_nothing(monkeypatch, fixtures):
    _, opened = fixtures
    open_binary = FileResolver._open_binary

    def second_open_fails(path):
        if opened:
            raise PermissionError("private filesystem failure")
        return open_binary(path)

    monkeypatch.setattr(FileResolver, "_open_binary", staticmethod(second_open_fails))
    client, sent = _client(monkeypatch, opened)
    files = {**_files(), "attachment": {"fixture": "second.txt"}}

    with pytest.raises(FileResolutionError):
        _run(_case(files=files), client)

    _assert_closed(opened)
    assert sent == []


@pytest.mark.parametrize("data", ["raw", None, {"a": None}, {"a": {}}, {"a": [[1]]}, {"a": (1,)}, {"a": b"bytes"}, {"a": io.BytesIO(b"bytes")}])
def test_multipart_resolved_data_shape_is_enforced_before_open(monkeypatch, fixtures, data):
    _, opened = fixtures
    client, sent = _client(monkeypatch, opened)

    with pytest.raises(FileResolutionError, match="Multipart data"):
        _run(_case(files=_files(), data=data), client)

    assert opened == sent == []


@pytest.mark.parametrize("replacement", [None, {"nested": "dictionary"}, ["nested", "list"], b"bytes"])
def test_multipart_data_is_validated_after_variable_resolution(monkeypatch, fixtures, replacement):
    _, opened = fixtures
    data = {"title": "before resolution"}
    resolve = VariableResolver.resolve

    def resolve_with_non_scalar(value):
        if value is data:
            return {"title": replacement}
        return resolve(value)

    monkeypatch.setattr(VariableResolver, "resolve", resolve_with_non_scalar)
    client, sent = _client(monkeypatch, opened)
    if isinstance(replacement, list):
        _run(_case(files=_files(), data=data), client)
        _assert_closed(opened)
        assert len(sent) == 1
    else:
        with pytest.raises(FileResolutionError, match="Multipart data"):
            _run(_case(files=_files(), data=data), client)
        assert opened == sent == []


@pytest.mark.parametrize("region,body", [("json", {"value": "{{title}}"}), ("data", {"value": "{{title}}"}), ("data", "{{title}}")])
def test_existing_json_and_data_only_resolution_remain_unchanged(monkeypatch, fixtures, region, body):
    _, opened = fixtures
    Context.set_variable("title", "Resolved Title")
    client, sent = _client(monkeypatch, opened)

    _run(_case(**{region: body}), client)

    assert len(sent) == 1
    prepared_body = sent[0].body
    if isinstance(prepared_body, bytes):
        prepared_body = prepared_body.decode("utf-8")
    assert "Resolved" in prepared_body
    assert "multipart/form-data" not in sent[0].headers.get("Content-Type", "")
    assert opened == []


def test_empty_files_keeps_data_only_contract_and_never_touches_root(monkeypatch, fixtures):
    _, opened = fixtures
    monkeypatch.setattr(FileResolver, "_fixture_root", "/unused/missing/root")
    client, sent = _client(monkeypatch, opened, defaults={"Content-Type": "text/plain"})

    _run(_case(files={}, data="raw data remains allowed"), client)

    assert sent[0].body == "raw data remains allowed"
    assert sent[0].headers["Content-Type"] == "text/plain"
    assert opened == []


@pytest.mark.parametrize("empty_files", [False, True])
def test_data_only_retains_existing_api_client_variable_resolution(monkeypatch, fixtures, empty_files):
    _, opened = fixtures
    Context.set_variable("first", "$data.final_value")
    Context.set_variable("data.final_value", "Final Value")
    client, sent = _client(monkeypatch, opened)
    body = {"data": {"title": "{{first}}"}}
    if empty_files:
        body["files"] = {}

    _run(_case(**body), client)

    assert sent[0].body == "title=Final+Value"
    assert opened == []


@pytest.mark.parametrize("raw", ["{{title}}", {"upload": "binary.bin"}, {"upload": ("name", io.BytesIO(b"raw"))}, {"upload": [{"fixture": "binary.bin"}]}])
def test_legacy_raw_files_are_rejected_without_variable_resolution(monkeypatch, fixtures, raw):
    _, opened = fixtures
    resolve = VariableResolver.resolve

    def never_resolve_files(value):
        assert value is not raw, "files metadata must never reach VariableResolver"
        return resolve(value)

    monkeypatch.setattr(VariableResolver, "resolve", never_resolve_files)
    client, sent = _client(monkeypatch, opened)

    with pytest.raises(FileResolutionError):
        _run(_case(files=raw), client)

    assert opened == sent == []
