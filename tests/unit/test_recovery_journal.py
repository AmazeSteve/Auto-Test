from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

from wizbank_api_test.services.user_update_state_validation import normalize
from wizbank_api_test.utils import recovery_journal as recovery


@pytest.fixture
def envelope():
    path = Path(__file__).resolve().parents[2] / "tests/fixtures/user_update_read_model.json"
    return {"target": "json", "payload": normalize(json.loads(path.read_text()), "zh-cn")}


def test_versioned_atomic_journal_contains_independent_normalized_material(tmp_path, envelope, monkeypatch):
    journal = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")
    events, replace, fsync = [], recovery.os.replace, recovery.os.fsync

    def observed_fsync(fd):
        events.append("fsync")
        return fsync(fd)

    def observed_replace(source, destination):
        events.append("replace")
        assert Path(source).parent == journal.directory
        assert not Path(destination).exists()
        assert json.loads(Path(source).read_text())["restore_payload"] == envelope
        return replace(source, destination)

    monkeypatch.setattr(recovery.os, "fsync", observed_fsync)
    monkeypatch.setattr(recovery.os, "replace", observed_replace)
    expected = deepcopy(envelope)
    path = journal.create("OFFLINE_CASE", "offline-user", envelope)
    assert events == ["fsync", "replace"]
    envelope["payload"]["usr_display_bil"] = "external change"
    record = json.loads(path.read_text())
    assert record["schema_version"] == 1 and record["status"] == "prepared"
    assert record["base_commit"] == recovery.BASE_COMMIT
    assert record["target_user_hash"] == recovery.sha256("offline-user")
    assert record["restore_payload_sha256"] == recovery.sha256(expected)
    assert record["restore_payload"] == expected
    assert "created_at" in record
    assert "offline-user" not in path.name
    assert list(journal.directory.iterdir()) == [path]
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0


def test_hash_is_deterministic_utf8_and_key_order_independent():
    first = {"b": None, "a": "测试"}
    second = {"a": "测试", "b": None}
    assert recovery.sha256(first) == recovery.sha256(second)
    assert recovery.stable_json(first) == '{"a":"测试","b":null}'


@pytest.mark.parametrize("success", [True, False])
def test_success_removes_sensitive_file_failure_retains_it(tmp_path, envelope, success):
    journal = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")
    path = journal.create("OFFLINE_CASE", "offline-user", envelope)
    journal.finish(success=success)
    assert path.exists() is not success
    assert journal.status == ("deleted" if success else "retained")
    assert "restore_payload" not in journal.summary()


@pytest.mark.parametrize("key", ["Cookie", "Authorization", "password", "admin_cookie", "session_token"])
def test_journal_rejects_embedded_credentials_before_write(tmp_path, envelope, key):
    envelope["payload"]["usr_extra_2"] = {key: "credential-must-not-be-written"}
    journal = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")
    with pytest.raises(ValueError, match="credential"):
        journal.create("OFFLINE_CASE", "offline-user", envelope)
    assert not journal.directory.exists()


def test_journal_record_excludes_auth_material_and_raw_target_metadata(tmp_path, envelope):
    journal = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")
    record = json.loads(journal.create("OFFLINE_CASE", "operator-target", envelope).read_text())
    assert set(record) == {"schema_version", "status", "case_id", "created_at", "base_commit",
                           "target_user_hash", "restore_payload_sha256", "restore_payload"}
    assert "operator-target" not in json.dumps(record)
    assert not {"Cookie", "Authorization", "password", "usr_pwd"} & set(record["restore_payload"]["payload"])


def test_replace_failure_is_pre_write_failure_and_cleans_temporary_file(tmp_path, envelope, monkeypatch):
    journal = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")

    def fail_replace(*args):
        raise OSError("offline simulated disk failure")

    monkeypatch.setattr(recovery.os, "replace", fail_replace)
    with pytest.raises(OSError):
        journal.create("OFFLINE_CASE", "offline-user", envelope)
    assert journal.path is None and list(journal.directory.iterdir()) == []


def test_journal_default_directory_is_runtime_only_and_git_ignored():
    root = Path(__file__).resolve().parents[2]
    assert recovery.DEFAULT_DIRECTORY == root / ".runtime/recovery"
    result = subprocess.run(["git", "check-ignore", "--no-index", ".runtime/recovery/offline.json"],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0


def test_another_invocation_is_rejected_without_changing_existing_recovery(tmp_path, envelope):
    first = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")
    second = recovery.RecoveryJournal(first.directory)
    first_path = first.create("OFFLINE_CASE", "offline-user", envelope)
    contents = first_path.read_bytes()
    with pytest.raises(recovery.PendingRecoveryError, match="count=1"):
        second.create("OFFLINE_CASE", "offline-user", envelope)
    assert second.path is None and first_path.read_bytes() == contents
    assert list(first.directory.iterdir()) == [first_path]


@pytest.mark.parametrize("directory", ["docs", "reports/allure-results", "reports/allure-report", "services"])
def test_journal_refuses_tracked_or_report_directories(directory):
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="ignored .runtime"):
        recovery.RecoveryJournal(root / directory)


def test_pending_detection_never_opens_or_prints_payload(tmp_path, monkeypatch):
    journal = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")
    journal.directory.mkdir(parents=True)
    path = journal.directory / "old.json"
    contents = b"private-payload-not-even-valid-json\xff"
    path.write_bytes(contents)

    def forbidden_read(*args, **kwargs):
        raise AssertionError("pending detection must not read journal content")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", forbidden_read)
        scoped.setattr(Path, "read_text", forbidden_read)
        scoped.setattr(Path, "read_bytes", forbidden_read)
        assert journal.pending_records() == (path,)
        assert journal.has_pending()
        with pytest.raises(recovery.PendingRecoveryError) as caught:
            journal.assert_no_pending()
        assert caught.value.count == 1 and caught.value.directory == journal.directory
        assert "private-payload" not in str(caught.value)
    assert path.read_bytes() == contents


def test_tmp_files_and_json_directories_are_not_pending_records(tmp_path, envelope):
    journal = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")
    journal.directory.mkdir(parents=True)
    temporary = journal.directory / ".journal-old.tmp"
    temporary.write_bytes(b"private-incomplete-payload")
    (journal.directory / "not-a-record.json").mkdir()
    assert journal.pending_records() == () and not journal.has_pending()
    journal.assert_no_pending()
    path = journal.create("OFFLINE_CASE", "offline-user", envelope)
    assert journal.pending_records() == (path,)
    assert temporary.read_bytes() == b"private-incomplete-payload"


def test_missing_recovery_directory_has_no_pending_records(tmp_path):
    journal = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")
    assert journal.pending_records() == () and not journal.has_pending()
    journal.assert_no_pending()
    assert not journal.directory.exists()


def test_pending_scan_error_is_not_treated_as_an_empty_directory(tmp_path, envelope, monkeypatch):
    journal = recovery.RecoveryJournal(tmp_path / ".runtime/recovery")

    def denied(*args):
        raise PermissionError("offline simulated directory access denial")

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(PermissionError):
        journal.create("OFFLINE_CASE", "offline-user", envelope)
    assert journal.path is None
