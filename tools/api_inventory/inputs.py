"""Read local directories/archives without extraction, execution or network."""

from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


def _safe_name(name):
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe archive member path")
    return path.as_posix()


def iter_files(asset, suffixes, *, nested_archives=False):
    """Yield relative member name + bytes in a stable order, never asset absolute paths."""
    asset = Path(asset)
    suffixes = {suffix.lower() for suffix in suffixes}

    def archive(data, prefix="", depth=0):
        try:
            with ZipFile(data) as zipped:
                members = sorted(zipped.infolist(), key=lambda member: member.filename)
                names = [_safe_name(member.filename) for member in members if not member.is_dir()]
                if len(names) != len(set(names)):
                    raise ValueError("Duplicate archive member names")
                for member in members:
                    name = _safe_name(member.filename)
                    if member.is_dir():
                        continue
                    suffix = PurePosixPath(name).suffix.lower()
                    if suffix in suffixes:
                        yield prefix + name, zipped.read(member)
                    elif nested_archives and suffix == ".zip":
                        if depth >= 3:
                            raise ValueError("Nested archive depth exceeds supported limit")
                        yield from archive(BytesIO(zipped.read(member)), prefix + name + "!", depth + 1)
        except BadZipFile as exc:
            raise ValueError("Invalid ZIP analysis input") from exc

    if asset.is_dir():
        for path in sorted(asset.rglob("*")):
            if not path.is_file():
                continue
            if not path.resolve().is_relative_to(asset.resolve()):
                raise ValueError("Analysis input symlink escapes input directory")
            name = path.relative_to(asset).as_posix()
            if path.suffix.lower() in suffixes:
                yield name, path.read_bytes()
            elif nested_archives and path.suffix.lower() == ".zip":
                yield from archive(path, name + "!")
    elif asset.is_file() and asset.suffix.lower() == ".zip":
        yield from archive(asset)
    else:
        raise ValueError("Analysis input must be a local directory or ZIP archive")
