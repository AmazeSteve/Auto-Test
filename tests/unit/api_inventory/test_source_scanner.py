"""Offline declaration and security regressions for Spring source candidates."""

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from tools.api_inventory.source_scanner import scan_source


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "api_inventory" / "source"


def _write_java(path, source):
    # Existing semantics fixtures must be legal Spring-importing source. New
    # provenance tests use write_text directly to exercise missing/foreign imports.
    path.write_text("import org.springframework.web.bind.annotation.*;\n"
                    "import org.springframework.stereotype.Controller;\n" + source,
                    encoding="utf-8")


@pytest.fixture(scope="module")
def scanned():
    return scan_source(FIXTURE)


def methods(result, name):
    return [record for record in result.records if record["java_method"] == name]


def test_class_and_method_path_arrays_form_cartesian_product(scanned):
    records = methods(scanned, "list")
    assert {record["runtime_path"] for record in records} == {
        "/app/v1/users/{id}", "/app/v1/members/{name}",
        "/app/v2/users/{id}", "/app/v2/members/{name}",
    }
    assert all(record["method"] == "GET" and record["resolution_status"] == "RESOLVED" for record in records)
    assert {record["route_fingerprint"] for record in records} == {
        "/v1/users/{}", "/v1/members/{}", "/v2/users/{}", "/v2/members/{}",
    }
    assert all(record["controller"] == "synthetic.ApiController" for record in records)
    assert all(isinstance(record["line"], int) and record["line"] > 0 for record in records)


def test_request_method_arrays_expand_methods_and_paths(scanned):
    records = methods(scanned, "multipleMethods")
    assert {(record["method"], record["runtime_path"]) for record in records} == {
        ("GET", "/app/v1/multi"), ("POST", "/app/v1/multi"),
        ("GET", "/app/v2/multi"), ("POST", "/app/v2/multi"),
    }
    assert len({record["api_uid"] for record in records}) == 4


def test_methodless_request_mapping_remains_ambiguous(scanned):
    records = methods(scanned, "unrestricted")
    assert len(records) == 2
    assert all(record["method"] == "ANY" and record["resolution_status"] == "AMBIGUOUS_METHOD" for record in records)
    assert any(issue["category"] == "AMBIGUOUS_METHOD" for issue in scanned.issues)


def test_shorthand_mapping_without_parentheses_maps_class_root(scanned):
    records = methods(scanned, "rootPost")
    assert {(record["method"], record["runtime_path"]) for record in records} == {
        ("POST", "/app/v1"), ("POST", "/app/v2"),
    }


def test_comments_and_strings_do_not_create_mapping_candidates(scanned):
    serialized = json.dumps(scanned.records)
    assert "line-comment-fake" not in serialized
    assert "block-comment-fake" not in serialized
    assert "quoted-fake" not in serialized
    assert not methods(scanned, "ordinary")
    records = methods(scanned, "commentMarkers")
    assert records[0]["mapping_conditions"]["params"] == ["mode!=off", "note=/*literal*/", "version=1"]
    assert records[0]["runtime_path"] == "/app/v1/url/literal"


def test_mapping_conditions_combine_and_override_by_spring_semantics(scanned):
    conditions = methods(scanned, "list")[0]["mapping_conditions"]
    assert conditions["params"] == ["!gone", "active", "mode!=off", "version=1"]
    assert conditions["consumes"] == ["application/json"]
    assert conditions["produces"] == ["text/plain"]
    assert "X-Scope=read" in conditions["headers"]
    assert methods(scanned, "multipleMethods")[0]["mapping_conditions"]["consumes"] == ["application/xml"]


@pytest.mark.parametrize("name,kind", [
    ("list", "HTTP_API"), ("json", "HTTP_API"), ("entity", "HTTP_API"),
    ("httpEntity", "HTTP_API"), ("view", "PAGE_ROUTE"), ("model", "PAGE_ROUTE"),
    ("unclear", "UNKNOWN_ROUTE"), ("object", "UNKNOWN_ROUTE"),
])
def test_controller_classification_requires_clear_evidence(scanned, name, kind):
    assert all(record["api_kind"] == kind for record in methods(scanned, name))


def test_class_and_method_deprecation(scanned):
    assert all(record["deprecated"] for record in methods(scanned, "old"))
    assert methods(scanned, "deprecatedClass")[0]["deprecated"]
    assert not methods(scanned, "list")[0]["deprecated"]


def test_mapping_without_controller_is_not_confirmed_active_exposure(scanned):
    record = methods(scanned, "unregistered")[0]
    assert record["active"] is False
    assert record["api_kind"] == "UNKNOWN_ROUTE"


def test_nested_controller_owns_its_mapping_prefix(scanned):
    record = methods(scanned, "own")[0]
    assert record["runtime_path"] == "/nested/own"
    assert record["controller"] == "synthetic.ApiController.NestedController"


@pytest.mark.parametrize("name", ["constant", "concatenation", "constantMethod", "constantCondition",
                                  "conflictingAliases", "conflictingMappings", "mixedArray", "unresolvedClass"])
def test_nonliteral_or_conflicting_mappings_remain_unresolved(scanned, name):
    records = methods(scanned, name)
    assert records
    assert all(record["resolution_status"] == "UNRESOLVED_JAVA_EXPRESSION" for record in records)
    assert all(record["identity"] is None and record["api_uid"] is None for record in records)
    assert any(issue["category"] == "UNRESOLVED_JAVA_EXPRESSION" for issue in scanned.issues)


@pytest.mark.parametrize("name", ["operation", "indirectOperation", "vendor", "vendorQualified"])
def test_custom_composed_annotations_are_candidates_not_resolved_routes(scanned, name):
    record = methods(scanned, name)[0]
    assert record["resolution_status"] == "UNRESOLVED_JAVA_EXPRESSION"
    assert record["runtime_path"] is None and record["api_uid"] is None
    assert "custom_composed_mapping" in record["unresolved_attributes"]


def test_sensitive_condition_values_are_hashed_without_losing_operators(scanned):
    serialized = json.dumps({"records": scanned.records, "issues": scanned.issues})
    for canary in ["SOURCE_AUTH_CANARY", "SOURCE_COOKIE_CANARY", "SOURCE_PASSWORD_CANARY",
                   "SOURCE_SECRET_CANARY", "BODY_PASSWORD_CANARY", "SECRET_CONSTANT", "SUFFIX_CONSTANT"]:
        assert canary not in serialized
    password_hash = hashlib.sha256(b"SOURCE_PASSWORD_CANARY").hexdigest()
    conditions = methods(scanned, "sensitiveConditions")[0]["mapping_conditions"]["params"]
    assert "password=sha256:" + password_hash in conditions
    assert any(condition.startswith("client_secret!=sha256:") for condition in conditions)
    assert not any(key in record for record in scanned.records for key in ["raw", "body", "annotation_args", "source"])


def test_directory_and_zip_inputs_produce_identical_safe_records(tmp_path, scanned):
    archive = tmp_path / "source.zip"
    with ZipFile(archive, "w") as zipped:
        for path in sorted(FIXTURE.glob("*.java")):
            zipped.write(path, path.name)
    other = scan_source(archive)
    assert other == scanned
    assert scanned.counts["source_files"] == 3
    assert scanned.counts["source_parse_errors"] == 0
    assert any("Inherited" in limitation for limitation in scanned.limitations)


def test_method_restrictions_inherit_and_union_when_both_present(tmp_path):
    _write_java(tmp_path / "Union.java", '''
        @RestController @RequestMapping(path="/base", method=RequestMethod.GET)
        class Union {
          @RequestMapping("/inherit") public Object inherit() { return null; }
          @RequestMapping(value="/union", method=RequestMethod.POST) public Object union() { return null; }
        }
    ''')
    result = scan_source(tmp_path)
    assert {record["method"] for record in methods(result, "inherit")} == {"GET"}
    assert {record["method"] for record in methods(result, "union")} == {"GET", "POST"}


def test_malformed_java_is_reported_without_input_contents(tmp_path):
    _write_java(tmp_path / "Malformed.java", '@RestController class Bad { @GetMapping("SOURCE_SECRET_CANARY")')
    result = scan_source(tmp_path)
    assert result.records == []
    assert result.counts["source_parse_errors"] == 1
    assert result.issues[0]["category"] == "SOURCE_PARSE_ERROR"
    assert "SOURCE_SECRET_CANARY" not in json.dumps(result.issues)


def test_empty_mapping_arrays_and_empty_annotation_parentheses(tmp_path):
    _write_java(tmp_path / "Empty.java", '''
      @RestController @RequestMapping(path={}) class Empty {
        @GetMapping() public void empty() {}
      }
    ''')
    record = scan_source(tmp_path).records[0]
    assert record["runtime_path"] == "/" and record["method"] == "GET"


def test_java_string_escapes_and_trailing_array_comma(tmp_path):
    _write_java(tmp_path / "Escapes.java", r'''
      @RestController class Escapes {
        @GetMapping(path={"/escaped\u0061", "/other",}, params={"value=a\\b",})
        public void escaped() {}
      }
    ''')
    result = scan_source(tmp_path)
    assert {record["runtime_path"] for record in result.records} == {"/escapeda", "/other"}
    assert result.records[0]["mapping_conditions"]["params"] == ["value=a\\b"]


@pytest.mark.parametrize("suffix", ["@", "@() class Bad {}"])
def test_declaration_parse_errors_drop_all_partial_file_records(tmp_path, suffix):
    _write_java(tmp_path / "Partial.java", '''
      @RestController class Valid {
        @GetMapping("/valid") public void valid() {}
      }
    ''' + suffix)
    result = scan_source(tmp_path)
    assert result.records == []
    assert result.counts["source_parse_errors"] == 1


def test_unicode_comment_preprocessing_cannot_hide_active_mapping(tmp_path):
    _write_java(tmp_path / "Unicode.java", r'''
      @RestController class Unicode {
        // \u000a @GetMapping("/hidden") public void hidden() {}
        @GetMapping("/ordinary") public void ordinary() {}
      }
    ''')
    result = scan_source(tmp_path)
    assert result.records == []
    assert result.counts["source_parse_errors"] == 1


def test_nested_custom_mapping_definition_is_reported_unresolved(tmp_path):
    _write_java(tmp_path / "NestedAnnotation.java", '''
      class Definitions {
        @RequestMapping(method=RequestMethod.POST) @interface Operation {}
      }
      @RestController class Routes {
        @Definitions.Operation public void operation() {}
      }
    ''')
    result = scan_source(tmp_path)
    assert result.records[0]["resolution_status"] == "UNRESOLVED_JAVA_EXPRESSION"
    assert result.records[0]["api_uid"] is None


def test_inherited_mapping_prefix_cannot_be_guessed(tmp_path):
    _write_java(tmp_path / "Inheritance.java", '''
      @RequestMapping("/base") class Parent {}
      @RestController class Child extends Parent {
        @GetMapping("/child") public Object child() { return null; }
      }
    ''')
    result = scan_source(tmp_path)
    assert result.records[0]["resolution_status"] == "UNRESOLVED_JAVA_EXPRESSION"
    assert result.records[0]["runtime_path"] is None and result.records[0]["api_uid"] is None
    assert any(issue["category"] == "INHERITED_MAPPING_UNSUPPORTED" for issue in result.issues)


@pytest.mark.parametrize("mapping", ["vendor.GetMapping", "org.springframework.web.bind.annotation.fake.GetMapping"])
def test_foreign_qualified_mapping_annotations_do_not_resolve(tmp_path, mapping):
    _write_java(tmp_path / "Foreign.java", '@RestController class Foreign { @' + mapping + '("/x") public void route() {} }')
    result = scan_source(tmp_path)
    assert result.records[0]["resolution_status"] == "UNRESOLVED_JAVA_EXPRESSION"


def test_explicit_foreign_mapping_import_does_not_resolve(tmp_path):
    _write_java(tmp_path / "Foreign.java", '''
      import vendor.GetMapping;
      @RestController class Foreign { @GetMapping("/x") public void route() {} }
    ''')
    assert scan_source(tmp_path).records[0]["resolution_status"] == "UNRESOLVED_JAVA_EXPRESSION"


@pytest.mark.parametrize("type_name", ["vendor.RestController", "vendor.Controller"])
def test_foreign_controller_stereotypes_do_not_prove_exposure(tmp_path, type_name):
    _write_java(tmp_path / "Foreign.java", '@' + type_name + ' class Foreign { @GetMapping("/x") public void route() {} }')
    record = scan_source(tmp_path).records[0]
    assert record["active"] is False and record["api_kind"] == "UNKNOWN_ROUTE"


@pytest.mark.parametrize("method", ["vendor.RequestMethod.GET", "org.springframework.web.bind.annotation.fake.RequestMethod.GET"])
def test_foreign_request_method_constants_do_not_resolve(tmp_path, method):
    _write_java(tmp_path / "Foreign.java", '@RestController class Foreign { @RequestMapping(path="/x", method=' + method + ') public void route() {} }')
    assert scan_source(tmp_path).records[0]["resolution_status"] == "UNRESOLVED_JAVA_EXPRESSION"


def test_foreign_response_types_do_not_prove_http_exposure(tmp_path):
    _write_java(tmp_path / "Foreign.java", '''
      import vendor.ResponseEntity;
      @Controller class Foreign {
        @GetMapping("/x") public ResponseEntity route() { return null; }
        @GetMapping("/y") @vendor.ResponseBody public String body() { return service.call(); }
      }
    ''')
    assert all(record["api_kind"] == "UNKNOWN_ROUTE" for record in scan_source(tmp_path).records)


@pytest.mark.parametrize("header", ["route()", "Object route() = 1"])
def test_malformed_mapped_method_declarations_cannot_emit_resolved_records(tmp_path, header):
    _write_java(tmp_path / "Malformed.java", '@RestController class Bad { @GetMapping("/x") ' + header + '; }')
    result = scan_source(tmp_path)
    assert result.records == []
    assert result.counts["source_parse_errors"] == 1


def test_method_named_after_class_is_not_mistaken_for_constructor(tmp_path):
    _write_java(tmp_path / "SameName.java", '''
      @RestController class SameName {
        public SameName() {}
        @GetMapping("/same") public void SameName() {}
      }
    ''')
    records = scan_source(tmp_path).records
    assert len(records) == 1 and records[0]["java_method"] == "SameName"
    assert records[0]["runtime_path"] == "/same"


def test_enum_constants_with_optional_semicolon_are_not_methods(tmp_path):
    _write_java(tmp_path / "Enums.java", '''
      enum Plain { FIRST, SECOND }
      enum Arguments { FIRST(1), SECOND(2); Arguments(int n) {} }
      @RestController class Routes {
        enum Nested { FIRST }
        @GetMapping("/valid") public Object valid() { return null; }
      }
    ''')
    result = scan_source(tmp_path)
    assert result.counts["source_parse_errors"] == 0
    assert len(result.records) == 1 and result.records[0]["java_method"] == "valid"


def test_unicode_letters_and_quotes_inside_comments_remain_comment_text(tmp_path):
    _write_java(tmp_path / "Unicode.java", r'''
      // \u0022 \u0061 harmless comment text
      @RestController class Routes {
        @GetMapping("/valid") public void valid() {}
      }
    ''')
    result = scan_source(tmp_path)
    assert result.counts["source_parse_errors"] == 0 and len(result.records) == 1


@pytest.mark.parametrize("path", ["#{config.path}", "/file-?.txt"])
def test_source_path_patterns_are_not_truncated_to_resolved_identities(tmp_path, path):
    _write_java(tmp_path / "Pattern.java", '@RestController class Pattern { @GetMapping("' + path + '") public void path() {} }')
    record = scan_source(tmp_path).records[0]
    assert record["resolution_status"] == "UNRESOLVED_JAVA_EXPRESSION"
    assert record["runtime_path"] is None and record["api_uid"] is None


def test_unresolved_path_cannot_expose_sensitive_literal_conditions(tmp_path):
    _write_java(tmp_path / "Secret.java", '''
      @RestController class Secret {
        @GetMapping(path=PATH_CONSTANT, headers="Authorization=SOURCE_AUTH_CANARY")
        public void unresolved() {}
      }
    ''')
    result = scan_source(tmp_path)
    assert result.records[0]["api_uid"] is None
    assert result.records[0]["mapping_conditions"]["headers"][0].startswith("Authorization=sha256:")
    assert "SOURCE_AUTH_CANARY" not in json.dumps({"records": result.records, "issues": result.issues})


def test_raw_digest_marker_condition_does_not_alias_a_secret_comparison(tmp_path):
    digest_literal = "sha256:" + hashlib.sha256(b"SOURCE_AUTH_CANARY").hexdigest()
    _write_java(tmp_path / "Secret.java", '''
      @RestController class Secret {
        @GetMapping(path="/same", headers="Authorization=SOURCE_AUTH_CANARY")
        public void raw() {}
        @GetMapping(path="/same", headers="Authorization=''' + digest_literal + '''")
        public void marker() {}
      }
    ''')
    result = scan_source(tmp_path)
    assert len({record["api_uid"] for record in result.records}) == 2


def test_generic_array_response_entity_is_still_clearly_http(tmp_path):
    _write_java(tmp_path / "Entity.java", '''
      import org.springframework.http.ResponseEntity;
      @Controller class Entity {
        @GetMapping("/download") public ResponseEntity<byte[]> download() { return response; }
      }
    ''')
    assert scan_source(tmp_path).records[0]["api_kind"] == "HTTP_API"


def test_get_handlers_retain_explicit_write_declaration_evidence(tmp_path):
    _write_java(tmp_path / "Operations.java", '''
      @RestController class Operations {
        @GetMapping("/course/resource/objective/delexamine/{id}")
        public Object delExamine() { return null; }
        @GetMapping("/detail/{id}") public Object getDetail() { return null; }
      }
    ''')
    result = scan_source(tmp_path)
    deletion = methods(result, "delExamine")[0]
    detail = methods(result, "getDetail")[0]
    assert deletion["method"] == "GET" and deletion["mutation_candidate"] is True
    assert deletion["mutation_evidence"] == "JAVA_OPERATION_DECLARATION"
    assert detail["method"] == "GET" and detail["mutation_candidate"] is False
    assert "mutation_evidence" not in detail
    assert all(record["resolution_status"] == "RESOLVED" for record in result.records)
    assert all("mutation_candidate" not in record["identity"] for record in result.records)


@pytest.mark.parametrize("operation", [
    "add", "addUser", "insertItem", "updateStatus", "updUser", "deleteItem", "delExamine",
    "removeItem", "createItem", "saveItem", "resetPassword", "set_status", "uploadFile", "importData",
    "enableItem", "disableItem", "activateItem", "deactivateItem", "grantRole", "revokeRole",
    "bindUser", "unbindUser",
])
def test_write_operation_prefix_requires_a_declaration_boundary(tmp_path, operation):
    _write_java(tmp_path / "Operations.java", '@RestController class Operations { @GetMapping("/same") public void ' + operation + '() {} }')
    record = scan_source(tmp_path).records[0]
    assert record["mutation_candidate"] is True
    assert record["mutation_evidence"] == "JAVA_OPERATION_DECLARATION"


@pytest.mark.parametrize("operation", ["address", "queryUpdateStatus", "getDetail", "setup", "delicate", "remover"])
def test_operation_prefix_does_not_guess_mutations_from_partial_words(tmp_path, operation):
    _write_java(tmp_path / "Operations.java", '@RestController class Operations { @GetMapping("/delete/in/url") public void ' + operation + '() {} }')
    record = scan_source(tmp_path).records[0]
    assert record["mutation_candidate"] is False
    assert "mutation_evidence" not in record


@pytest.mark.parametrize("imports,mapping", [
    ("import org.springframework.web.bind.annotation.GetMapping;", "GetMapping"),
    ("import org.springframework.web.bind.annotation.*;", "GetMapping"),
    ("", "org.springframework.web.bind.annotation.GetMapping"),
])
def test_proven_mapping_imports_and_fqn_keep_resolved_identity(tmp_path, imports, mapping):
    from tools.api_inventory.classifier import classify
    from tools.api_inventory.models import ScanResult
    from tools.api_inventory.reconciler import reconcile

    (tmp_path / "Routes.java").write_text(imports + '''
      @org.springframework.web.bind.annotation.RestController class Routes {
        @''' + mapping + '''("/x") public Object route() { return null; }
      }
    ''', encoding="utf-8")
    result = scan_source(tmp_path)
    record = result.records[0]
    assert record["resolution_status"] == "RESOLVED" and record["method"] == "GET"
    assert record["api_kind"] == "HTTP_API" and record["active"] is True
    assert not result.issues
    records, _, _ = reconcile(result, ScanResult())
    assert classify(records)[0]["eligibility"] == "ELIGIBLE"


@pytest.mark.parametrize("imports,declaration", [
    ("import com.vendor.GetMapping;", ""),
    ("import com.vendor.*;", ""),
    ("", ""),
    ("", "@interface GetMapping {}"),
    ("import org.springframework.web.bind.annotation.*;", "@interface GetMapping {}"),
    ("import org.springframework.web.bind.annotation.*; import com.vendor.*;", ""),
])
def test_unproven_same_named_mapping_cannot_enter_canonical_denominator(tmp_path, imports, declaration):
    from tools.api_inventory.coverage import calculate_coverage
    from tools.api_inventory.classifier import classify
    from tools.api_inventory.reconciler import reconcile

    (tmp_path / "Routes.java").write_text('''package example;
      ''' + imports + declaration + '''
      @org.springframework.web.bind.annotation.RestController class Routes {
        @GetMapping("/x") public Object route() { return null; }
      }
    ''', encoding="utf-8")
    result = scan_source(tmp_path)
    assert result.records[0]["api_uid"] is None
    assert result.records[0]["identity"] is None
    assert result.records[0]["resolution_status"] == "UNRESOLVED_JAVA_EXPRESSION"
    assert "annotation_provenance" in result.records[0]["unresolved_attributes"]
    assert result.counts["source_unproven_mapping_annotations"] == 1
    assert any(issue["category"] == "UNPROVEN_ANNOTATION_PROVENANCE" for issue in result.issues)
    records, _, _ = reconcile(result)
    assert calculate_coverage(classify(records))["static_case_coverage"]["denominator"] == 0


def test_other_file_same_package_declaration_shadows_spring_wildcard(tmp_path):
    (tmp_path / "GetMapping.java").write_text('package example; @interface GetMapping {}', encoding="utf-8")
    (tmp_path / "Routes.java").write_text('''package example;
      import org.springframework.web.bind.annotation.*;
      @RestController class Routes { @GetMapping("/x") public void route() {} }
    ''', encoding="utf-8")
    result = scan_source(tmp_path)
    assert result.records[0]["api_uid"] is None
    assert result.counts["source_unproven_mapping_annotation_files"] == 1


def test_other_package_custom_same_name_does_not_poison_proven_spring_mapping(tmp_path):
    (tmp_path / "GetMapping.java").write_text('''package com.vendor;
      @org.springframework.web.bind.annotation.RequestMapping @interface GetMapping {}
    ''', encoding="utf-8")
    (tmp_path / "Routes.java").write_text('''package example;
      import org.springframework.web.bind.annotation.GetMapping;
      @org.springframework.web.bind.annotation.RestController class Routes {
        @GetMapping("/x") public Object route() { return null; }
      }
    ''', encoding="utf-8")
    result = scan_source(tmp_path)
    assert result.records[0]["resolution_status"] == "RESOLVED"
    assert result.records[0]["api_uid"]


@pytest.mark.parametrize("imports,method,status", [
    ("import org.springframework.web.bind.annotation.RequestMethod;", "RequestMethod.GET", "RESOLVED"),
    ("import org.springframework.web.bind.annotation.*;", "RequestMethod.GET", "RESOLVED"),
    ("import com.vendor.RequestMethod;", "RequestMethod.GET", "UNRESOLVED_JAVA_EXPRESSION"),
    ("import com.vendor.*;", "RequestMethod.GET", "UNRESOLVED_JAVA_EXPRESSION"),
    ("", "RequestMethod.GET", "UNRESOLVED_JAVA_EXPRESSION"),
    ("import com.vendor.RequestMethod;", "org.springframework.web.bind.annotation.RequestMethod.GET", "RESOLVED"),
])
def test_request_method_requires_its_own_provenance(tmp_path, imports, method, status):
    (tmp_path / "Routes.java").write_text(imports + '''
      @org.springframework.web.bind.annotation.RestController class Routes {
        @org.springframework.web.bind.annotation.RequestMapping(path="/x", method=''' + method + ''')
        public Object route() { return null; }
      }
    ''', encoding="utf-8")
    record = scan_source(tmp_path).records[0]
    assert record["resolution_status"] == status
    assert bool(record["api_uid"]) == (status == "RESOLVED")
    if status == "RESOLVED":
        assert record["method"] == "GET"


def test_same_package_request_method_enum_cannot_gain_spring_semantics(tmp_path):
    (tmp_path / "Routes.java").write_text('''package example;
      import org.springframework.web.bind.annotation.*;
      enum RequestMethod { GET }
      @RestController class Routes {
        @RequestMapping(path="/x", method=RequestMethod.GET) public void route() {}
      }
    ''', encoding="utf-8")
    assert scan_source(tmp_path).records[0]["api_uid"] is None


@pytest.mark.parametrize("imports", ["import org.springframework.stereotype.Controller;",
                                     "import org.springframework.stereotype.*;"])
def test_controller_import_and_java_lang_deprecated_are_proven(tmp_path, imports):
    (tmp_path / "Routes.java").write_text(imports + '''
      @Controller class Routes {
        @Deprecated @org.springframework.web.bind.annotation.ResponseBody
        @org.springframework.web.bind.annotation.GetMapping("/x") public Object route() { return null; }
      }
    ''', encoding="utf-8")
    record = scan_source(tmp_path).records[0]
    assert record["api_kind"] == "HTTP_API" and record["active"] and record["deprecated"]


@pytest.mark.parametrize("imports,declaration", [("import com.vendor.*;", ""), ("", "@interface RestController {}")])
def test_unproven_controller_simple_name_cannot_establish_http_exposure(tmp_path, imports, declaration):
    from tools.api_inventory.classifier import classify
    from tools.api_inventory.reconciler import reconcile

    (tmp_path / "Routes.java").write_text(imports + declaration + '''
      @RestController class Routes {
        @org.springframework.web.bind.annotation.GetMapping("/x") public Object route() { return null; }
      }
    ''', encoding="utf-8")
    result = scan_source(tmp_path)
    assert result.records[0]["api_kind"] == "UNKNOWN_ROUTE" and not result.records[0]["active"]
    records, _, _ = reconcile(result)
    assert classify(records)[0]["eligibility"] == "UNRESOLVED"


def test_fqn_mapping_survives_local_simple_name_shadow(tmp_path):
    (tmp_path / "Routes.java").write_text('''
      @interface GetMapping {}
      @org.springframework.web.bind.annotation.RestController class Routes {
        @org.springframework.web.bind.annotation.GetMapping("/x") public void route() {}
      }
    ''', encoding="utf-8")
    assert scan_source(tmp_path).records[0]["resolution_status"] == "RESOLVED"


def _write_return_route(path, *, return_type, imports="", declarations="", return_value="null",
                        controller="@Controller", class_parameters="", method_parameters=""):
    path.write_text('''package return_example;
      import org.springframework.stereotype.Controller;
      import org.springframework.web.bind.annotation.GetMapping;
      ''' + imports + declarations + controller + ''' class Routes''' + class_parameters + ''' {
        @GetMapping("/return-provenance") public ''' + method_parameters + return_type + ''' route() {
          return ''' + return_value + ''';
        }
      }
    ''', encoding="utf-8")


@pytest.mark.parametrize("imports,return_type", [
    ("import org.springframework.http.ResponseEntity;", "ResponseEntity"),
    ("import org.springframework.http.*;", "ResponseEntity"),
    ("", "org.springframework.http.ResponseEntity"),
    ("import org.springframework.http.HttpEntity;", "HttpEntity"),
    ("import org.springframework.http.*;", "HttpEntity"),
    ("", "org.springframework.http.HttpEntity"),
    ("import org.springframework.http.ResponseEntity;", "ResponseEntity<byte[]>"),
])
def test_proven_http_return_types_provide_http_semantics(tmp_path, imports, return_type):
    _write_return_route(tmp_path / "Routes.java", imports=imports, return_type=return_type)
    record = scan_source(tmp_path).records[0]
    assert record["api_kind"] == "HTTP_API" and record["active"]
    assert record["resolution_status"] == "RESOLVED"


@pytest.mark.parametrize("imports,declaration,return_type", [
    ("", "", "ResponseEntity"),
    ("import vendor.ResponseEntity;", "", "ResponseEntity"),
    ("import vendor.*;", "", "ResponseEntity"),
    ("", "class ResponseEntity {}", "ResponseEntity"),
    ("import org.springframework.http.*;", "class ResponseEntity {}", "ResponseEntity"),
    ("import org.springframework.http.*; import vendor.*;", "", "ResponseEntity"),
    ("", "", "vendor.ResponseEntity"),
    ("", "", "HttpEntity"),
    ("import vendor.HttpEntity;", "", "HttpEntity"),
])
def test_unproven_http_return_types_cannot_enter_eligible_denominator(tmp_path, imports, declaration, return_type):
    from tools.api_inventory.reconciler import reconcile
    from tools.api_inventory.classifier import classify
    from tools.api_inventory.coverage import calculate_coverage

    _write_return_route(tmp_path / "Routes.java", imports=imports, declarations=declaration,
                        return_type=return_type)
    result = scan_source(tmp_path)
    assert result.records[0]["api_kind"] == "UNKNOWN_ROUTE" and result.records[0]["active"]
    records, _, _ = reconcile(result)
    assert classify(records)[0]["eligibility"] == "UNRESOLVED"
    assert calculate_coverage(records)["static_case_coverage"]["denominator"] == 0


@pytest.mark.parametrize("name", ["ResponseEntity", "String"])
def test_other_file_same_package_return_type_shadow_is_unknown(tmp_path, name):
    (tmp_path / (name + ".java")).write_text('package return_example; class ' + name + ' {}', encoding="utf-8")
    _write_return_route(tmp_path / "Routes.java", imports="import org.springframework.http.*;",
                        return_type=name, return_value='"literal-view"')
    assert scan_source(tmp_path).records[0]["api_kind"] == "UNKNOWN_ROUTE"


def test_fqn_http_return_type_is_independent_of_simple_name_shadows(tmp_path):
    _write_return_route(tmp_path / "Routes.java", declarations="class ResponseEntity {}",
                        imports="import vendor.ResponseEntity;",
                        return_type="org.springframework.http.ResponseEntity<byte[]>")
    assert scan_source(tmp_path).records[0]["api_kind"] == "HTTP_API"


@pytest.mark.parametrize("name,package", [("ModelAndView", "org.springframework.web.servlet"),
                                       ("View", "org.springframework.web.servlet"),
                                       ("RedirectView", "org.springframework.web.servlet.view")])
@pytest.mark.parametrize("form", ["explicit", "wildcard", "fqn", "bare", "shadow", "foreign"])
def test_view_return_types_require_their_own_provenance(tmp_path, name, package, form):
    imports = "import " + package + (".*;" if form == "wildcard" else "." + name + ";") if form in {"explicit", "wildcard"} else ""
    if form == "foreign":
        imports = "import vendor." + name + ";"
    _write_return_route(tmp_path / "Routes.java", imports=imports,
                        declarations="class " + name + " {}" if form == "shadow" else "",
                        return_type=package + "." + name if form == "fqn" else name)
    assert scan_source(tmp_path).records[0]["api_kind"] == (
        "PAGE_ROUTE" if form in {"explicit", "wildcard", "fqn"} else "UNKNOWN_ROUTE")


@pytest.mark.parametrize("return_type", ["String", "java.lang.String"])
def test_unshadowed_java_lang_string_keeps_literal_view_semantics(tmp_path, return_type):
    _write_return_route(tmp_path / "Routes.java", return_type=return_type, return_value='"literal-view"')
    assert scan_source(tmp_path).records[0]["api_kind"] == "PAGE_ROUTE"


@pytest.mark.parametrize("imports,declarations,return_type,kind", [
    ("", "class String {}", "String", "UNKNOWN_ROUTE"),
    ("import vendor.String;", "", "String", "UNKNOWN_ROUTE"),
    ("import vendor.String;", "class String {}", "java.lang.String", "PAGE_ROUTE"),
    ("import java.lang.String;", "class String {}", "String", "UNKNOWN_ROUTE"),
])
def test_string_implicit_import_cannot_bypass_visible_shadow(tmp_path, imports, declarations, return_type, kind):
    _write_return_route(tmp_path / "Routes.java", imports=imports, declarations=declarations,
                        return_type=return_type, return_value='"literal-view"')
    assert scan_source(tmp_path).records[0]["api_kind"] == kind


@pytest.mark.parametrize("class_parameters,method_parameters,return_type,kind", [
    ("<ResponseEntity>", "", "ResponseEntity", "UNKNOWN_ROUTE"),
    ("", "<ResponseEntity> ", "ResponseEntity", "UNKNOWN_ROUTE"),
    ("<@vendor.Nullable ResponseEntity>", "", "ResponseEntity", "UNKNOWN_ROUTE"),
    ("<ResponseEntity>", "", "org.springframework.http.ResponseEntity<byte[]>", "HTTP_API"),
    ("<T extends ResponseEntity>", "", "ResponseEntity", "HTTP_API"),
])
def test_visible_type_parameters_shadow_imported_simple_return_types(tmp_path, class_parameters,
                                                                     method_parameters, return_type, kind):
    _write_return_route(tmp_path / "Routes.java", imports="import org.springframework.http.ResponseEntity;",
                        class_parameters=class_parameters, method_parameters=method_parameters,
                        return_type=return_type)
    assert scan_source(tmp_path).records[0]["api_kind"] == kind


@pytest.mark.parametrize("controller", ["@org.springframework.web.bind.annotation.RestController",
                                       "@Controller @org.springframework.web.bind.annotation.ResponseBody"])
def test_body_producer_annotations_do_not_depend_on_return_type_provenance(tmp_path, controller):
    _write_return_route(tmp_path / "Routes.java", declarations="class ResponseEntity {}",
                        return_type="ResponseEntity", controller=controller)
    assert scan_source(tmp_path).records[0]["api_kind"] == "HTTP_API"


def test_return_type_wildcard_policy_does_not_relax_annotation_provenance(tmp_path):
    (tmp_path / "Routes.java").write_text('''
      import org.springframework.web.bind.annotation.*;
      import org.springframework.http.*;
      @org.springframework.stereotype.Controller class Routes {
        @GetMapping("/x") public ResponseEntity route() { return null; }
      }
    ''', encoding="utf-8")
    result = scan_source(tmp_path)
    assert result.records[0]["api_uid"] is None
    assert "annotation_provenance" in result.records[0]["unresolved_attributes"]
