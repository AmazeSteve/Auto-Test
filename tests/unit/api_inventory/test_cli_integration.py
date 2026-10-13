"""Exercise complete local scans, coverage and reports through the CLI boundary."""

from pathlib import Path
import shutil
import socket
from zipfile import ZipFile

import pytest
import requests
import yaml

from tools.api_inventory.cli import analyze, load_overrides, main
from tools.api_inventory.identity import api_uid, make_identity


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/api_inventory/integration"


def inputs(tmp_path):
    source, postman, repo = (tmp_path / name for name in ("source", "postman", "repo"))
    source.mkdir()
    postman.mkdir()
    (repo / "data/endpoints").mkdir(parents=True)
    (repo / "data/cases").mkdir()
    shutil.copyfile(FIXTURES / "Items.java", source / "Items.java")
    shutil.copyfile(FIXTURES / "canary.request.yaml", postman / "canary.request.yaml")
    endpoint = [{"id": "ITEM", "method": "GET", "path": "/app/api/items/{id}", "auth": "admin"}]
    cases = [{"id": "FIRST", "endpoint": "ITEM"}, {"id": "SECOND", "endpoint": "ITEM"},
             {"id": "DISABLED", "endpoint": "ITEM", "path": "/api/unused", "enabled": False}]
    (repo / "data/endpoints/items.yaml").write_text(yaml.safe_dump(endpoint), encoding="utf-8")
    (repo / "data/cases/items.yaml").write_text(yaml.safe_dump(cases), encoding="utf-8")
    return source, postman, repo


def deny_network(*args, **kwargs):
    raise AssertionError("Offline analyzer attempted network access")


def test_complete_directory_run_is_offline_secret_safe_and_deterministic(tmp_path, monkeypatch, capsys):
    source, postman, repo = inputs(tmp_path)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(requests.Session, "request", deny_network)
    output = tmp_path / "reports"
    assert main(["--source", str(source), "--postman", str(postman), "--repo-root", str(repo),
                 "--output-dir", str(output)]) == 0
    first = {path.name: path.read_bytes() for path in output.iterdir()}
    result = analyze(source, postman, repo, output)
    assert first == {path.name: path.read_bytes() for path in output.iterdir()}
    assert set(first) == {"api_inventory.json", "api_inventory.csv", "coverage_summary.json", "unresolved.csv",
                          "duplicates.csv", "api_inventory_report.md", "priority_queue.csv"}
    text = b"".join(first.values()).decode("utf-8") + capsys.readouterr().out
    assert "CANARY_" not in text
    assert "Authorization" in text and "Cookie" in text and "client_secret" in text
    summary = result["summary"]
    assert summary["registry_utilization"]["value"] == 1
    assert summary["static_case_coverage"]["numerator"] == 1
    assert summary["static_case_coverage"]["denominator"] == 2
    assert summary["counts"]["enabled_cases"] == 2
    assert summary["counts"]["disabled_cases"] == 1
    assert any(issue["category"] == "AMBIGUOUS_METHOD" for issue in result["issues"])


def test_zip_and_directory_inputs_produce_identical_reports(tmp_path):
    source, postman, repo = inputs(tmp_path)
    archives = []
    for directory in (source, postman):
        archive = directory.with_suffix(".zip")
        with ZipFile(archive, "w") as stream:
            for path in directory.iterdir():
                stream.write(path, path.name)
        archives.append(archive)
    out1, out2 = tmp_path / "dir-reports", tmp_path / "zip-reports"
    analyze(source, postman, repo, out1)
    analyze(*archives, repo, out2)
    assert {p.name: p.read_bytes() for p in out1.iterdir()} == {p.name: p.read_bytes() for p in out2.iterdir()}


def test_default_reports_are_local_runtime_artifacts(tmp_path):
    source, postman, repo = inputs(tmp_path)
    analyze(source, postman, repo)
    assert (repo / ".runtime/api_inventory/coverage_summary.json").is_file()


@pytest.mark.parametrize("destination", ["data", "data/endpoints/reports", "data/cases/reports", "core", "services", "utils", ".git"])
def test_report_output_cannot_overwrite_production_assets(tmp_path, destination):
    source, postman, repo = inputs(tmp_path)
    with pytest.raises(ValueError):
        analyze(source, postman, repo, repo / destination)


def test_report_output_cannot_modify_source_inputs(tmp_path):
    source, postman, repo = inputs(tmp_path)
    with pytest.raises(ValueError):
        analyze(source, postman, repo, source / "reports")


@pytest.mark.parametrize("bad_document", ["{{CANARY_SECRET", "apis: {bad: {eligibility: ELIGIBLE}}", "password: CANARY_SECRET"])
def test_malformed_or_invalid_inputs_fail_without_echoing_values(tmp_path, capsys, bad_document):
    source, postman, repo = inputs(tmp_path)
    overrides = tmp_path / "invalid.yaml"
    overrides.write_text(bad_document, encoding="utf-8")
    assert main(["--source", str(source), "--postman", str(postman), "--repo-root", str(repo),
                 "--overrides", str(overrides)]) == 2
    captured = capsys.readouterr()
    assert "CANARY_SECRET" not in captured.out + captured.err


def test_bad_postman_yaml_is_fatal_and_secret_safe(tmp_path, capsys):
    source, postman, repo = inputs(tmp_path)
    (postman / "canary.request.yaml").write_text("[CANARY_MALFORMED", encoding="utf-8")
    assert main(["--source", str(source), "--postman", str(postman), "--repo-root", str(repo)]) == 2
    assert "CANARY_MALFORMED" not in capsys.readouterr().err


def test_explicit_override_is_validated_and_visible(tmp_path):
    source, postman, repo = inputs(tmp_path)
    uid = api_uid(make_identity("wizbank", "GET", "/api/unused"))
    override = tmp_path / "override.yaml"
    override.write_text(yaml.safe_dump({"apis": {uid: {"business_risk": 25, "eligibility": "EXCLUDED"}}}), encoding="utf-8")
    result = analyze(source, postman, repo, tmp_path / "reports", override)
    record = next(record for record in result["records"] if record["api_uid"] == uid)
    assert record["eligibility"] == "EXCLUDED"
    assert record["applied_override"]["business_risk"] == 25


def test_absent_identity_override_fails_instead_of_silently_applying(tmp_path):
    source, postman, repo = inputs(tmp_path)
    override = tmp_path / "override.yaml"
    override.write_text(yaml.safe_dump({"apis": {"0" * 64: {"eligibility": "ELIGIBLE"}}}), encoding="utf-8")
    with pytest.raises(ValueError):
        analyze(source, postman, repo, overrides_path=override)


@pytest.mark.parametrize("fields", [{"business_risk": True}, {"readonly": "true"}, {"module_weight": -1},
                                   {"api_kind": "GUESS"}, {"password": "CANARY"}, {}])
def test_override_schema_never_accepts_arbitrary_values(tmp_path, fields):
    path = tmp_path / "override.yaml"
    path.write_text(yaml.safe_dump({"apis": {"0" * 64: fields}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_overrides(path)
