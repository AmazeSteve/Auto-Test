"""Analysis inputs cannot escape archives, alias names or mutate external files."""

from io import BytesIO
from zipfile import ZipFile

import pytest

from tools.api_inventory.inputs import iter_files
from tools.api_inventory.path_normalizer import normalize_path


@pytest.mark.parametrize("name", ["../outside.java", "/absolute.java", "nested/../../outside.java"])
def test_archive_traversal_is_rejected_without_extraction(tmp_path, name):
    archive = tmp_path / "input.zip"
    with ZipFile(archive, "w") as stream:
        stream.writestr(name, "class Example {}")
    with pytest.raises(ValueError):
        list(iter_files(archive, {".java"}))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["input.zip"]


@pytest.mark.parametrize("names", [("a/b.java", "a\\b.java"), ("a/b.java", "a/./b.java")])
def test_normalized_duplicate_archive_names_are_fatal(tmp_path, names):
    archive = tmp_path / "input.zip"
    with ZipFile(archive, "w") as stream:
        for name in names:
            stream.writestr(name, "class Example {}")
    with pytest.raises(ValueError):
        list(iter_files(archive, {".java"}))


def test_nested_zip_is_read_in_memory_in_deterministic_member_order(tmp_path):
    inner = BytesIO()
    with ZipFile(inner, "w") as stream:
        stream.writestr("b.yaml", "b")
        stream.writestr("a.yaml", "a")
    archive = tmp_path / "input.zip"
    with ZipFile(archive, "w") as stream:
        stream.writestr("nested.zip", inner.getvalue())
    assert list(iter_files(archive, {".yaml"}, nested_archives=True)) == [("nested.zip!a.yaml", b"a"), ("nested.zip!b.yaml", b"b")]
    assert list(iter_files(archive, {".yaml"})) == []


def test_escaping_directory_symlink_is_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.java"
    outside.write_text("class Private {}", encoding="utf-8")
    try:
        (source / "escape.java").symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Symlink creation is unavailable without Windows symlink privilege")
        raise
    with pytest.raises(ValueError):
        list(iter_files(source, {".java"}))


@pytest.mark.parametrize("path", ["ftp://user:CANARY@example.invalid/x", "file://private/file"])
def test_non_http_authority_is_rejected_instead_of_leaking_into_path(path):
    with pytest.raises(ValueError):
        normalize_path(path)


def test_redirect_query_values_do_not_affect_path_validation():
    assert normalize_path("{{Host-be}}/app/x?redirect=https://private.example/path")["canonical_path"] == "/x"
