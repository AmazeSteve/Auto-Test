"""Static file grammar, confinement, and scoped ownership without HTTP."""

from collections import OrderedDict
from copy import deepcopy
import io
import os
from pathlib import Path
import socket
import stat
import subprocess
from types import SimpleNamespace

import pytest
import requests

from wizbank_api_test.core.file_resolver import FileResolver, FileResolutionError


BINARY = b"\x00\xff\r\n" + "中文 sentinel".encode("utf-8")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("B5 resolver tests must not contact a network")
    monkeypatch.setattr(requests.Session, "send", reject)
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    monkeypatch.setattr(socket, "create_connection", reject)


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "PRIVATE_ABSOLUTE_PATH_SENTINEL" / "fixtures 中文"
    path.mkdir(parents=True)
    monkeypatch.setattr(FileResolver, "_fixture_root", path)
    return path


def fixture_file(root, reference="course/demo.bin"):
    path = root / reference
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BINARY)
    return path


class TrackedFile:
    def __init__(self, handle, *, close_error=False):
        self.handle = handle
        self.close_count = 0
        self.read_count = 0
        self.close_error = close_error

    @property
    def closed(self):
        return self.handle.closed

    def fileno(self):
        return self.handle.fileno()

    def read(self, size=-1):
        self.read_count += 1
        return self.handle.read(size)

    def close(self):
        self.close_count += 1
        self.handle.close()
        if self.close_error:
            raise OSError("PRIVATE_ABSOLUTE_PATH_SENTINEL")


def tracked_open(monkeypatch, *, fail_name=None, close_error_name=None):
    opened = []
    def open_binary(path):
        if path.name == fail_name:
            raise PermissionError(str(path))
        handle = TrackedFile(path.open("rb"), close_error=path.name == close_error_name)
        opened.append(handle)
        return handle
    monkeypatch.setattr(FileResolver, "_open_binary", staticmethod(open_binary))
    return opened


@pytest.mark.parametrize("reference", ["course/demo.bin", "course/中文 文件.bin", "folder a/file 01.xlsx"])
def test_static_relative_unicode_and_space_references_preserve_binary(root, reference):
    fixture_file(root, reference)
    spec = {"upload": {"fixture": reference}}
    original = deepcopy(spec)
    with FileResolver.resolve(spec) as resolved:
        filename, handle = resolved.request_files["upload"]
        assert filename == reference.rsplit("/", 1)[-1]
        assert handle.read() == BINARY
        assert handle.closed is False
        assert resolved.evidence == [{"field": "upload", "filename": filename, "size": len(BINARY), "content_type": None}]
        assert not hasattr(handle, "name")
    assert handle.closed is True
    assert spec == original


@pytest.mark.parametrize("reference", [
    "", "/absolute/file", "//server/share", "C:\\file", "C:/file", "C:file",
    "\\\\server\\share", "\\\\?\\C:\\file", "../file", "a/../../file", "./file",
    "a/./file", "a//file", "a/file/", "a\\file", "a:b/file", ".", "..",
    "{{file}}", "course/{{name}}.bin", "$data.path", "a/$data.path", "$timestamp_ms",
    "a/$timestamp_ms.bin", "a/\x00.bin", "a/\rfile", "a/\nfile", "https://example.invalid/file",
])
def test_unsafe_reference_rejected_without_any_open(root, monkeypatch, reference):
    def forbidden(*args):
        raise AssertionError("Invalid lexical references must not open files")
    monkeypatch.setattr(FileResolver, "_open_binary", forbidden)
    with pytest.raises(FileResolutionError):
        FileResolver.resolve({"upload": {"fixture": reference}})


@pytest.mark.parametrize("reference", [None, 1, False, b"file.bin", ["file.bin"], {"path": "x"}])
def test_reference_must_be_static_string(root, reference):
    with pytest.raises(FileResolutionError):
        FileResolver.resolve({"upload": {"fixture": reference}})


@pytest.mark.parametrize("shape", [None, [], (), b"x", "path", io.BytesIO(b"x")])
def test_files_must_be_mapping(shape):
    with pytest.raises(FileResolutionError):
        FileResolver.resolve(shape)


def test_unknown_descriptor_keys_and_raw_tuples_are_rejected():
    for descriptor in ({"fixture": "x", "root": "/private"}, ("file", io.BytesIO(b"x")), [{"fixture": "x"}]):
        with pytest.raises(FileResolutionError):
            FileResolver.resolve({"upload": descriptor})


def test_empty_files_does_not_access_missing_root(tmp_path, monkeypatch):
    monkeypatch.setattr(FileResolver, "_fixture_root", tmp_path / "missing")
    with FileResolver.resolve({}) as resolved:
        assert resolved.request_files == {}
        assert resolved.evidence == []


def test_nonempty_files_fail_safely_when_root_missing(tmp_path, monkeypatch):
    private = tmp_path / "PRIVATE_ABSOLUTE_PATH_SENTINEL"
    monkeypatch.setattr(FileResolver, "_fixture_root", private)
    with pytest.raises(FileResolutionError) as error:
        with FileResolver.resolve({"upload": {"fixture": "x"}}):
            pytest.fail("Missing root cannot enter")
    assert str(private) not in str(error.value)
    assert "PRIVATE_ABSOLUTE_PATH_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("reference", ["Course/demo.bin", "course/Demo.bin", "course/missing.bin"])
def test_missing_and_case_mismatched_components_are_rejected(root, reference):
    fixture_file(root)
    with pytest.raises(FileResolutionError):
        with FileResolver.resolve({"upload": {"fixture": reference}}):
            pytest.fail("Logical component case must match")


def test_unicode_is_not_silently_normalized(root):
    fixture_file(root, "course/e\u0301.bin")
    with pytest.raises(FileResolutionError):
        with FileResolver.resolve({"upload": {"fixture": "course/\u00e9.bin"}}):
            pytest.fail("Unicode normalization is not part of the contract")


def test_directory_is_not_a_file(root, monkeypatch):
    fixture_file(root)
    opened = tracked_open(monkeypatch)
    with pytest.raises(FileResolutionError):
        with FileResolver.resolve({"upload": {"fixture": "course"}}):
            pytest.fail("Directory cannot be uploaded")
    assert opened == []


def test_posix_fifo_rejected_without_blocking_or_open(root, monkeypatch):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable on this platform")
    os.mkfifo(root / "pipe")
    opened = tracked_open(monkeypatch)
    with pytest.raises(FileResolutionError):
        with FileResolver.resolve({"upload": {"fixture": "pipe"}}):
            pytest.fail("FIFO must be rejected before open")
    assert opened == []


@pytest.mark.parametrize("escape", [False, True])
@pytest.mark.parametrize("directory", [False, True])
def test_symlink_components_are_rejected_even_inside_root(root, tmp_path, escape, directory):
    target = tmp_path / "outside" if escape else root / "inside"
    target.mkdir()
    (target / "file.bin").write_bytes(BINARY)
    link = root / "linked"
    try:
        link.symlink_to(target if directory else target / "file.bin", target_is_directory=directory)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Symlink creation is unavailable without Windows symlink privilege")
        raise
    reference = "linked/file.bin" if directory else "linked"
    with pytest.raises(FileResolutionError):
        with FileResolver.resolve({"upload": {"fixture": reference}}):
            pytest.fail("Detected symlinks cannot be used")


def test_symlink_fixture_root_is_rejected(root, tmp_path, monkeypatch):
    fixture_file(root)
    alias = tmp_path / "root-link"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Symlink creation is unavailable without Windows symlink privilege")
        raise
    monkeypatch.setattr(FileResolver, "_fixture_root", alias)
    with pytest.raises(FileResolutionError):
        with FileResolver.resolve({"upload": {"fixture": "course/demo.bin"}}):
            pytest.fail("Root links cannot erase provenance")


def test_detected_reparse_attribute_is_rejected(root, monkeypatch):
    fixture_file(root)
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == root / "course":
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(FileResolutionError, match="reparse"):
        with FileResolver.resolve({"upload": {"fixture": "course/demo.bin"}}):
            pytest.fail("Detected reparse attributes must reject on every platform")


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows junction creation requires Windows; reparse and canonical gates are tested separately")
def test_actual_windows_junction_is_rejected_when_creation_is_available(root, tmp_path):
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "file.bin").write_bytes(BINARY)
    link = root / "junction"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, timeout=10,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable on this host")
    try:
        with pytest.raises(FileResolutionError, match="reparse"):
            with FileResolver.resolve({"upload": {"fixture": "junction/file.bin"}}):
                pytest.fail("A detected junction cannot enter a multipart scope")
    finally:
        # Remove the junction itself, not the target tree.
        link.rmdir()


def test_canonical_containment_is_independent_of_link_detection(root, tmp_path, monkeypatch):
    path = fixture_file(root)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(BINARY)
    original = Path.resolve
    def resolve(candidate, *args, **kwargs):
        return outside if candidate == path else original(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(FileResolutionError, match="escapes"):
        with FileResolver.resolve({"upload": {"fixture": "course/demo.bin"}}):
            pytest.fail("Containment remains mandatory")


def test_permission_error_is_safe(root, monkeypatch):
    fixture_file(root)
    opened = tracked_open(monkeypatch, fail_name="demo.bin")
    with pytest.raises(FileResolutionError) as error:
        with FileResolver.resolve({"upload": {"fixture": "course/demo.bin"}}):
            pytest.fail("Open permission failure must reject")
    assert opened == []
    assert "PRIVATE_ABSOLUTE_PATH_SENTINEL" not in str(error.value)
    assert str(root) not in str(error.value)


def test_partial_open_failure_closes_every_previous_handle_exactly_once(root, monkeypatch):
    for name in ("a.bin", "b.bin", "c.bin"):
        fixture_file(root, name)
    opened = tracked_open(monkeypatch, fail_name="c.bin")
    spec = OrderedDict((name, {"fixture": name + ".bin"}) for name in ("a", "b", "c"))
    with pytest.raises(FileResolutionError):
        with FileResolver.resolve(spec):
            pytest.fail("Partial open must not enter request scope")
    assert len(opened) == 2
    assert all(handle.closed and handle.close_count == 1 for handle in opened)


@pytest.mark.parametrize("consumer_failure", [False, True])
def test_success_and_consumer_failure_close_all_handles_once(root, monkeypatch, consumer_failure):
    fixture_file(root)
    opened = tracked_open(monkeypatch)
    def consume():
        with FileResolver.resolve({"upload": {"fixture": "course/demo.bin"}}):
            assert len(opened) == 1 and not opened[0].closed
            if consumer_failure:
                raise RuntimeError("consumer failed")
    if consumer_failure:
        with pytest.raises(RuntimeError, match="consumer failed"):
            consume()
    else:
        consume()
    assert opened[0].closed and opened[0].close_count == 1


def test_opened_handle_is_verified_regular_and_closed_on_failure(root, monkeypatch):
    fixture_file(root)
    opened = tracked_open(monkeypatch)
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0))
    with pytest.raises(FileResolutionError, match="regular"):
        with FileResolver.resolve({"upload": {"fixture": "course/demo.bin"}}):
            pytest.fail("Opened nonregular handles cannot enter")
    assert opened[0].closed and opened[0].close_count == 1


def test_close_failure_still_attempts_all_other_closes(root, monkeypatch):
    fixture_file(root, "a.bin")
    fixture_file(root, "b.bin")
    opened = tracked_open(monkeypatch, close_error_name="b.bin")
    with pytest.raises(FileResolutionError, match="closed"):
        with FileResolver.resolve({"a": {"fixture": "a.bin"}, "b": {"fixture": "b.bin"}}):
            pass
    assert len(opened) == 2
    assert all(handle.closed and handle.close_count == 1 for handle in opened)


def test_scope_is_single_use(root):
    fixture_file(root)
    scope = FileResolver.resolve({"upload": {"fixture": "course/demo.bin"}})
    with scope:
        pass
    with pytest.raises(FileResolutionError, match="reused"):
        with scope:
            pytest.fail("Scope cannot reopen files")


def test_explicit_filename_and_mime_are_only_transport_metadata(root):
    fixture_file(root)
    with FileResolver.resolve({"attachment": {"fixture": "course/demo.bin", "filename": "用户 数据.pdf", "content_type": "application/custom"}}) as resolved:
        filename, handle, mime = resolved.request_files["attachment"]
        assert filename == "用户 数据.pdf" and mime == "application/custom"
        assert handle.read() == BINARY
        assert resolved.evidence == [{"field": "attachment", "filename": filename, "size": len(BINARY), "content_type": mime}]


def test_evidence_uses_stat_without_reading_or_hashing_file(root, monkeypatch):
    fixture_file(root)
    opened = tracked_open(monkeypatch)
    with FileResolver.resolve({"upload": {"fixture": "course/demo.bin"}}) as resolved:
        assert opened[0].read_count == 0
        assert resolved.evidence[0]["size"] == len(BINARY)
        assert "sha256" not in resolved.evidence[0]
        assert resolved.request_files["upload"][1].read() == BINARY
        assert opened[0].read_count == 1
    assert opened[0].close_count == 1
