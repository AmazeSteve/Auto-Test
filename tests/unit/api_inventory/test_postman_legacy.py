"""Offline Postman parsing, dispatch identity and value-redaction regression."""

import json
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from tools.api_inventory.legacy import is_legacy, legacy_signature
from tools.api_inventory.postman_scanner import scan_postman


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "api_inventory" / "postman"


def _request(tmp_path, name="request.request.yaml", **overrides):
    request = {"$kind": "http-request", "name": "Synthetic request",
               "method": "GET", "url": "{{Host-226}}/app/v2/items"}
    request.update(overrides)
    (tmp_path / name).write_text(yaml.safe_dump(request), encoding="utf-8")
    return request


def test_actual_yaml_schema_sanitized_regression():
    result = scan_postman(FIXTURES)
    record = next(record for record in result.records if record["request_name"] == "Synthetic format regression")
    assert record["method"] == "GET"
    assert record["runtime_path"] == "/app/v2/items/{{itemId}}"
    assert record["canonical_path"] == "/v2/items/{{itemId}}"
    assert record["route_fingerprint"] == "/v2/items/{}"
    assert record["host_form"] == "HOST_VARIABLE"
    assert record["query_keys"] == ["pageNum", "pdate", "token"]
    assert record["header_keys"] == ["Authorization", "Content-Type", "Cookie"]
    assert record["body_type"] == "json"
    assert record["body_keys"] == ["clientsecret", "items", "label", "password", "profile"]


def test_all_secret_canaries_absent_from_all_scan_outputs():
    serialized = json.dumps(asdict(scan_postman(FIXTURES)), sort_keys=True)
    assert "CANARY_" not in serialized
    assert "example.invalid" not in serialized
    assert "Bearer" not in serialized
    assert '"password"' in serialized
    assert '"clientsecret"' in serialized
    assert '"Cookie"' in serialized
    assert '"Authorization"' in serialized


def test_standard_collection_nested_items_and_safe_metadata():
    result = scan_postman(FIXTURES)
    record = next(record for record in result.records if record["request_name"] == "Synthetic standard request")
    assert record["method"] == "POST"
    assert record["canonical_path"] == "/v2/items"
    assert record["host_form"] == "ABSOLUTE_URL"
    assert record["body_type"] == "json"
    assert record["body_keys"] == ["clientsecret", "password"]
    assert record["query_keys"] == ["search", "token"]
    assert record["header_keys"] == ["Authorization", "Cookie"]


def test_empty_urls_are_issues_and_create_no_api():
    result = scan_postman(FIXTURES)
    assert result.counts["postman_requests"] == 3
    assert result.counts["postman_empty_urls"] == 1
    assert len(result.records) == 2
    assert [issue["category"] for issue in result.issues] == ["POSTMAN_EMPTY_URL"]


@pytest.mark.parametrize("url", ["{{Host-226}}/app/v2/items", "{{Host-226}}app/v2/items",
                                  "https://example.invalid/app/v2/items?token=CANARY_VALUE#CANARY_FRAGMENT",
                                  "HTTPS://user:CANARY_CREDENTIAL@example.invalid/app/v2/items?token=CANARY_VALUE",
                                  "//user:CANARY_CREDENTIAL@example.invalid/app/v2/items?token=CANARY_VALUE",
                                  "/app//v2/items/"])
def test_host_and_query_normalization(tmp_path, url):
    _request(tmp_path, url=url)
    record = scan_postman(tmp_path).records[0]
    assert record["canonical_path"] == "/v2/items"
    assert "CANARY_" not in json.dumps(record)


def test_query_values_do_not_change_identity(tmp_path):
    _request(tmp_path, "one.request.yaml", url="/app/v2/items?pageNum=1&pdate=one")
    _request(tmp_path, "two.request.yaml", url="/app/v2/items?pageNum=900&pdate=two")
    first, second = scan_postman(tmp_path).records
    assert first["api_uid"] == second["api_uid"]
    assert first["dispatch_signature"] == []


def test_url_valued_query_is_never_interpreted_as_transport_scheme(tmp_path):
    _request(tmp_path, url="{{Host-226}}/app/v2/items?redirect=https://user:CANARY_PASSWORD@example.invalid/private")
    record = scan_postman(tmp_path).records[0]
    assert record["canonical_path"] == "/v2/items"
    assert record["query_keys"] == ["redirect"]
    assert "CANARY_" not in json.dumps(record)
    assert "example.invalid" not in json.dumps(record)


def test_true_duplicate_request_evidence_is_preserved(tmp_path):
    request = _request(tmp_path, "first.request.yaml")
    (tmp_path / "second.request.yaml").write_text(yaml.safe_dump(request), encoding="utf-8")
    result = scan_postman(tmp_path)
    assert len(result.records) == 2
    assert result.records[0]["api_uid"] == result.records[1]["api_uid"]
    assert result.counts["packaging_duplicates"] == 0
    assert {record["file"] for record in result.records} == {"first.request.yaml", "second.request.yaml"}


def test_nested_zip_packaging_copies_are_counted_separately(tmp_path):
    data = yaml.safe_dump({"$kind": "http-request", "name": "Synthetic packaging copy",
                           "method": "GET", "url": "/app/v2/items"}).encode()
    nested = BytesIO()
    with ZipFile(nested, "w") as archive:
        archive.writestr("team/copy.request.yaml", data)
    asset = tmp_path / "postman.zip"
    with ZipFile(asset, "w") as archive:
        archive.writestr("collections/team/copy.request.yaml", data)
        archive.writestr("collections/collections.zip", nested.getvalue())
    result = scan_postman(asset)
    assert result.counts["raw_postman_requests"] == 2
    assert result.counts["postman_requests"] == 1
    assert result.counts["packaging_duplicates"] == 1
    assert [record["file"] for record in result.records] == ["collections/team/copy.request.yaml"]


def test_nested_archive_distinct_request_paths_are_preserved(tmp_path):
    data = yaml.safe_dump({"$kind": "http-request", "name": "Synthetic duplicate operation",
                           "method": "GET", "url": "/app/v2/items"}).encode()
    nested = BytesIO()
    with ZipFile(nested, "w") as archive:
        archive.writestr("team/second.request.yaml", data)
    asset = tmp_path / "postman.zip"
    with ZipFile(asset, "w") as archive:
        archive.writestr("collections/team/first.request.yaml", data)
        archive.writestr("collections/collections.zip", nested.getvalue())
    result = scan_postman(asset)
    assert len(result.records) == 2
    assert result.counts["packaging_duplicates"] == 0


def test_distinct_suffix_paths_in_same_nested_archive_are_preserved(tmp_path):
    data = yaml.safe_dump({"$kind": "http-request", "name": "Synthetic operation",
                           "method": "GET", "url": "/app/v2/items"}).encode()
    nested = BytesIO()
    with ZipFile(nested, "w") as archive:
        archive.writestr("team/request.request.yaml", data)
        archive.writestr("other/team/request.request.yaml", data)
    asset = tmp_path / "postman.zip"
    with ZipFile(asset, "w") as archive:
        archive.writestr("collections/collections.zip", nested.getvalue())
    result = scan_postman(asset)
    assert len(result.records) == 2
    assert result.counts["packaging_duplicates"] == 0


@pytest.mark.parametrize("body, expected_type, expected_keys", [
    ({"type": "urlencoded", "content": {"password": "CANARY_PASSWORD", "clientsecret": "CANARY_SECRET"}},
     "urlencoded", ["clientsecret", "password"]),
    ({"type": "urlencoded", "content": "password=CANARY_PASSWORD&clientsecret=CANARY_SECRET"},
     "urlencoded", ["clientsecret", "password"]),
    ({"type": "formdata", "content": [{"key": "password", "value": "CANARY_PASSWORD"},
                                            {"key": "skip", "value": "CANARY_SKIP", "disabled": True}]},
     "formdata", ["password"]),
    ({"type": "json", "content": {"password": "CANARY_PASSWORD"}}, "json", ["password"]),
    ({"type": "json", "content": [{"password": "CANARY_PASSWORD"}]}, "json", ["password"]),
    ({"type": "text", "content": "CANARY_TEXT"}, "text", []),
    ({"type": "CANARY_BODY_TYPE", "content": "CANARY_TEXT"}, "unknown", []),
])
def test_safe_body_types_and_keys(tmp_path, body, expected_type, expected_keys):
    _request(tmp_path, method="POST", body=body)
    record = scan_postman(tmp_path).records[0]
    assert record["body_type"] == expected_type
    assert record["body_keys"] == expected_keys
    assert "CANARY_" not in json.dumps(record)


def test_standard_url_object_without_raw(tmp_path):
    document = {"info": {"name": "Synthetic collection"}, "item": [{"name": "Synthetic structured URL", "request": {
        "method": "GET", "url": {"host": ["{{Host-226}}"], "path": ["app", "v2", "items"],
                                    "query": [{"key": "token", "value": "CANARY_TOKEN"}]}}}]}
    (tmp_path / "collection.json").write_text(json.dumps(document), encoding="utf-8")
    record = scan_postman(tmp_path).records[0]
    assert record["canonical_path"] == "/v2/items"
    assert record["host_form"] == "HOST_VARIABLE"
    assert record["query_keys"] == ["token"]


def test_dict_query_and_list_headers_are_supported(tmp_path):
    _request(tmp_path, queryParams={"password": "CANARY_QUERY_PASSWORD"}, headers=[
        {"key": "Cookie", "value": "CANARY_COOKIE"},
        {"key": "Disabled", "value": "CANARY_DISABLED_HEADER", "disabled": True}])
    record = scan_postman(tmp_path).records[0]
    assert record["query_keys"] == ["password"]
    assert record["header_keys"] == ["Cookie"]
    assert "CANARY_" not in json.dumps(record)


def test_unknown_methods_are_reported_without_guessing(tmp_path):
    _request(tmp_path, method="CANARY_BAD_METHOD")
    result = scan_postman(tmp_path)
    assert result.records == []
    assert result.issues[0]["category"] == "POSTMAN_UNRESOLVED_METHOD"
    assert "CANARY_" not in json.dumps(asdict(result))


def test_invalid_parse_error_does_not_quote_secret_values(tmp_path):
    (tmp_path / "malformed.yaml").write_text('value: [CANARY_SECRET\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid Postman analysis document") as error:
        scan_postman(tmp_path)
    assert "CANARY_" not in str(error.value)


@pytest.mark.parametrize("path", ["/servlet/qdbAction", "/app/servlet/Dispatcher", "/app/servlet/aeAction"])
def test_only_known_legacy_transport_paths(path):
    assert is_legacy(path)
    assert not is_legacy(path + "/other")


def test_qdb_cmd_differentiates_logical_operations():
    first, status = legacy_signature("/app/servlet/qdbAction", {"cmd": "readItems", "pageNum": "1"})
    second, _ = legacy_signature("/servlet/qdbAction", {"cmd": "readOtherItems", "pageNum": "1"})
    assert status == "RESOLVED"
    assert first == ["cmd=readItems"]
    assert first != second
    assert legacy_signature("/servlet/qdbAction", {"cmd": "readItems", "pageNum": "999", "pdate": "other"})[0] == first


def test_dispatcher_requires_module_and_cmd():
    assert legacy_signature("/app/servlet/Dispatcher", {"cmd": "readItems", "module": "training"}) == (
        ["cmd=readItems", "module=training"], "RESOLVED")
    assert legacy_signature("/servlet/Dispatcher", {"cmd": "readItems"}) == ([], "LEGACY_UNRESOLVED")
    assert legacy_signature("/servlet/Dispatcher", {"module": "training"}) == ([], "LEGACY_UNRESOLVED")


def test_ae_action_requires_proven_cmd_and_ignores_action():
    assert legacy_signature("/servlet/aeAction", {"cmd": "readItems", "action": "other"}) == (["cmd=readItems"], "RESOLVED")
    assert legacy_signature("/servlet/aeAction", {"action": "readItems"}) == ([], "LEGACY_UNRESOLVED")


@pytest.mark.parametrize("value", [None, "", "{{cmd}}", "${cmd}", "{cmd}", "Bearer CANARY_BEARER",
                                   "CANARY_CLIENT_SECRET", "CANARY_TOKEN", "CANARY_PASSWORD",
                                   "CANARY_API_KEY", "CANARY_SESSION_ID", "CANARY_PWD",
                                   "abcdef012345abcdef012345abcdef012345", "read/items", "read items", " readItems ", 12])
def test_missing_nonliteral_and_credential_dispatch_values_remain_unresolved(value):
    assert legacy_signature("/servlet/qdbAction", {"cmd": value}) == ([], "LEGACY_UNRESOLVED")


def test_conflicting_dispatch_values_remain_unresolved():
    assert legacy_signature("/servlet/qdbAction", {"cmd": ["readItems", "readOtherItems"]}) == ([], "LEGACY_UNRESOLVED")
    assert legacy_signature("/servlet/qdbAction", {"cmd": ["readItems", "readItems"]}) == (["cmd=readItems"], "RESOLVED")


def test_legacy_query_dispatch_is_decoded_and_ordinary_values_are_discarded(tmp_path):
    _request(tmp_path, url="/app/servlet/qdbAction?cmd=read%49tems&pageNum=CANARY_PAGE&pdate=CANARY_DATE")
    record = scan_postman(tmp_path).records[0]
    assert record["dispatch_signature"] == ["cmd=readItems"]
    assert record["identity"]["dispatch_signature"] == ["cmd=readItems"]
    assert "CANARY_" not in json.dumps(record)


def test_form_body_can_supply_legacy_dispatch_fields(tmp_path):
    _request(tmp_path, method="POST", url="/app/servlet/Dispatcher", body={"type": "urlencoded", "content": {
        "cmd": "readItems", "module": "training", "password": "CANARY_PASSWORD"}})
    record = scan_postman(tmp_path).records[0]
    assert record["dispatch_signature"] == ["cmd=readItems", "module=training"]
    assert record["body_keys"] == ["cmd", "module", "password"]
    assert "CANARY_" not in json.dumps(record)


@pytest.mark.parametrize("structured_field", ["queryParams", "url.query"])
def test_disabled_structured_dispatch_does_not_resolve_from_raw_url(tmp_path, structured_field):
    query = [{"key": "cmd", "value": "readItems", "disabled": True}]
    url = "/app/servlet/qdbAction?cmd=readItems"
    overrides = {"url": {"raw": url, "query": query}} if structured_field == "url.query" else {
        "url": url, "queryParams": query}
    _request(tmp_path, **overrides)
    record = scan_postman(tmp_path).records[0]
    assert record["resolution_status"] == "LEGACY_UNRESOLVED"
    assert record["dispatch_signature"] == []
    assert record["query_keys"] == []


def test_explicit_query_field_overrides_its_raw_representation(tmp_path):
    _request(tmp_path, url="/app/servlet/qdbAction?cmd=oldOperation", queryParams={"cmd": "readItems"})
    record = scan_postman(tmp_path).records[0]
    assert record["resolution_status"] == "RESOLVED"
    assert record["dispatch_signature"] == ["cmd=readItems"]


def test_file_body_part_is_not_a_dispatch_literal(tmp_path):
    _request(tmp_path, method="POST", url="/app/servlet/qdbAction", body={"type": "formdata", "content": [
        {"key": "cmd", "value": "readItems", "type": "file", "src": "CANARY_FILE_PATH"}]})
    record = scan_postman(tmp_path).records[0]
    assert record["body_keys"] == ["cmd"]
    assert record["resolution_status"] == "LEGACY_UNRESOLVED"
    assert "CANARY_" not in json.dumps(record)


def test_disabled_body_does_not_supply_dispatch_identity(tmp_path):
    _request(tmp_path, method="POST", url="/app/servlet/qdbAction", body={
        "disabled": True, "type": "urlencoded", "content": {"cmd": "readItems", "password": "CANARY_PASSWORD"}})
    record = scan_postman(tmp_path).records[0]
    assert record["body_type"] == "none"
    assert record["body_keys"] == []
    assert record["resolution_status"] == "LEGACY_UNRESOLVED"


@pytest.mark.parametrize("document", [
    '$kind: http-request\nmethod: POST\nurl: /app/v2/items\nbody:\n  type: json\n  content: &body\n    self: *body\n    password: CANARY_PASSWORD\n',
    'info: {name: Synthetic recursive collection}\nitem: &items\n  - item: *items\n',
])
def test_recursive_yaml_structures_fail_without_serializing_values(tmp_path, document):
    (tmp_path / "recursive.yaml").write_text(document, encoding="utf-8")
    with pytest.raises(ValueError, match="Cyclic Postman") as error:
        scan_postman(tmp_path)
    assert "CANARY_" not in str(error.value)


def test_standard_url_shorthand_is_counted_and_reports_missing_method(tmp_path):
    document = {"info": {"name": "Synthetic collection"},
                "item": [{"name": "Synthetic shorthand", "request": "https://example.invalid/app/v2/items"}]}
    (tmp_path / "collection.json").write_text(json.dumps(document), encoding="utf-8")
    result = scan_postman(tmp_path)
    assert result.counts["postman_requests"] == 1
    assert result.records == []
    assert result.issues[0]["category"] == "POSTMAN_UNRESOLVED_METHOD"


def test_json_body_does_not_guess_servlet_dispatch_parameters(tmp_path):
    _request(tmp_path, method="POST", url="/app/servlet/qdbAction", body={"type": "json", "content": {"cmd": "readItems"}})
    result = scan_postman(tmp_path)
    assert result.records[0]["resolution_status"] == "LEGACY_UNRESOLVED"
    assert "api_uid" not in result.records[0]
    assert result.issues[0]["category"] == "LEGACY_UNRESOLVED"


def test_missing_legacy_dispatch_is_evidence_without_api_identity(tmp_path):
    _request(tmp_path, url="/app/servlet/aeAction?cmd={{command}}&pdate=CANARY_DATE")
    result = scan_postman(tmp_path)
    assert len(result.records) == 1
    assert result.records[0]["dispatch_signature"] == []
    assert result.records[0]["resolution_status"] == "LEGACY_UNRESOLVED"
    assert "api_uid" not in result.records[0]
    assert "CANARY_" not in json.dumps(asdict(result))


def test_scan_output_is_deterministic():
    assert asdict(scan_postman(FIXTURES)) == asdict(scan_postman(FIXTURES))
