"""Static fixture references and request-scoped binary multipart ownership.

This boundary does not resolve variables, perform HTTP, or inspect credentials.
Only CaseRunner's main request borrows the handles owned by this scope.
"""

from collections.abc import Mapping
from contextlib import ExitStack
import os
from pathlib import Path, PureWindowsPath
import stat
import unicodedata


class FileResolutionError(ValueError):
    """Safe, stable multipart input or fixture failure."""


def _static_string(value):
    return (
        isinstance(value, str) and bool(value)
        and not any(token in value for token in ("{{", "}}", "$data.", "$timestamp_ms"))
        and not any(unicodedata.category(character) == "Cc" for character in value)
    )


def _validate_reference(reference):
    if not _static_string(reference):
        raise FileResolutionError("Multipart fixture must be a static relative reference")
    windows = PureWindowsPath(reference)
    if (reference.startswith("/") or windows.drive or windows.root
            or "\\" in reference or ":" in reference
            or any(component in ("", ".", "..") for component in reference.split("/"))):
        raise FileResolutionError("Multipart fixture reference is unsafe")


def _validate_files(spec):
    if not isinstance(spec, Mapping):
        raise FileResolutionError("Multipart files must be a Mapping")
    for field, descriptor in spec.items():
        if not _static_string(field) or not field.strip():
            raise FileResolutionError("Multipart field must be a non-empty static string")
        if not isinstance(descriptor, Mapping):
            raise FileResolutionError("Multipart file descriptor must be a Mapping")
        if set(descriptor) - {"fixture", "filename", "content_type"}:
            raise FileResolutionError("Multipart file descriptor has unknown keys")
        if "fixture" not in descriptor:
            raise FileResolutionError("Multipart file descriptor requires fixture")
        _validate_reference(descriptor["fixture"])
        if "filename" in descriptor:
            filename = descriptor["filename"]
            if (not _static_string(filename) or filename in (".", "..")
                    or any(character in filename for character in ("/", "\\", ":"))):
                raise FileResolutionError("Multipart filename must be a static basename")
        if "content_type" in descriptor:
            mime = descriptor["content_type"]
            if not _static_string(mime) or not mime.strip():
                raise FileResolutionError("Multipart content_type must be a non-empty static string")


def _validate_data(data):
    if not isinstance(data, Mapping):
        raise FileResolutionError("Multipart data must be a Mapping")
    for key, value in data.items():
        if not _static_string(key) or not key.strip():
            raise FileResolutionError("Multipart data keys must be non-empty static strings")
        values = value if isinstance(value, list) else [value]
        if any(not isinstance(item, (str, int, float, bool)) for item in values):
            raise FileResolutionError("Multipart data values must be scalars or lists of scalars")


class _SafeBinaryFile:
    """Expose only the binary operations Requests needs, with a path-free repr."""

    __slots__ = ("_handle",)

    def __init__(self, handle):
        self._handle = handle

    def __repr__(self):
        return "<MultipartBinaryFile closed=" + str(self.closed) + ">"

    @property
    def closed(self):
        return self._handle.closed

    def read(self, size=-1):
        try:
            return self._handle.read(size)
        except (OSError, ValueError):
            raise FileResolutionError("Multipart fixture could not be read") from None

    def close(self):
        if not self.closed:
            try:
                self._handle.close()
            except OSError:
                raise FileResolutionError("Multipart fixture could not be closed") from None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class FileResolver:
    """Resolve static descriptors; own every handle until the scope exits.

    The production root is fixed. Tests may monkeypatch ``_fixture_root``; no
    Case/YAML field can choose a root. A scope is single-use, including failure.
    """

    _fixture_root = Path(__file__).resolve().parents[1] / "data" / "files"

    def __init__(self, files_spec):
        try:
            _validate_files(files_spec)
        except FileResolutionError as error:
            files_spec = None
            raise FileResolutionError(str(error)) from None
        self._spec = {field: dict(descriptor) for field, descriptor in files_spec.items()}
        self._stack = ExitStack()
        self._entered = False
        self.request_files = {}
        self.evidence = []

    def __repr__(self):
        return "<FileResolver>"

    @classmethod
    def validate_files_spec(cls, files_spec):
        try:
            _validate_files(files_spec)
        except FileResolutionError as error:
            files_spec = None
            raise FileResolutionError(str(error)) from None

    @classmethod
    def validate_multipart_data(cls, data):
        try:
            _validate_data(data)
        except FileResolutionError as error:
            data = None
            raise FileResolutionError(str(error)) from None

    @staticmethod
    def ensure_no_content_type(*header_maps):
        try:
            for headers in header_maps:
                if headers is not None and any(str(key).casefold() == "content-type" for key in headers):
                    raise FileResolutionError("Multipart Content-Type must be owned by Requests")
        except FileResolutionError as error:
            header_maps = ()
            raise FileResolutionError(str(error)) from None

    @classmethod
    def resolve(cls, files_spec):
        try:
            return cls(files_spec)
        except FileResolutionError as error:
            files_spec = None
            raise FileResolutionError(str(error)) from None

    @staticmethod
    def _check_component(path):
        info = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse:
            raise FileResolutionError("Multipart fixture links and reparse points are not supported")
        return info

    @classmethod
    def _validated_path(cls, reference):
        root = Path(cls._fixture_root)
        # Check the supplied root and its components before canonicalization,
        # which would otherwise erase evidence of a symlink/junction.
        for component in reversed((root, *root.parents)):
            cls._check_component(component)
        canonical_root = root.resolve(strict=True)
        if not canonical_root.is_dir():
            raise FileResolutionError("Multipart fixture root must be a directory")
        current = root
        for component in reference.split("/"):
            with os.scandir(current) as entries:
                if component not in {entry.name for entry in entries}:
                    raise FileResolutionError("Multipart fixture component is missing or has incorrect case")
            current = current / component
            info = cls._check_component(current)
        candidate = current.resolve(strict=True)
        if not candidate.is_relative_to(canonical_root):
            raise FileResolutionError("Multipart fixture escapes the fixed root")
        if not stat.S_ISREG(info.st_mode):
            raise FileResolutionError("Multipart fixture must be a regular file")
        return candidate

    @staticmethod
    def _open_binary(path):
        return path.open("rb")

    def _open_files(self):
        try:
            for field, descriptor in self._spec.items():
                candidate = self._validated_path(descriptor["fixture"])
                handle = self._stack.enter_context(_SafeBinaryFile(self._open_binary(candidate)))
                opened = os.fstat(handle._handle.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise FileResolutionError("Opened multipart fixture must be a regular file")
                filename = descriptor.get("filename", descriptor["fixture"].rsplit("/", 1)[-1])
                mime = descriptor.get("content_type")
                self.request_files[field] = ((filename, handle, mime) if mime is not None else (filename, handle))
                self.evidence.append({"field": field, "filename": filename, "size": opened.st_size, "content_type": mime})
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            # A known safe category is retained; raw filesystem exceptions are
            # deliberately detached from public tracebacks.
            if isinstance(error, FileResolutionError):
                raise FileResolutionError(str(error)) from None
            raise FileResolutionError("Multipart fixture could not be resolved or opened") from None

    def __enter__(self):
        if self._entered:
            raise FileResolutionError("Multipart file scope cannot be reused")
        self._entered = True
        try:
            self._open_files()
        except FileResolutionError as error:
            self._stack.close()
            raise FileResolutionError(str(error)) from None
        return self

    def __exit__(self, exc_type, exc, traceback):
        # ExitStack attempts every close even if one close raises. No reliance
        # on garbage collection, Session.close(), or process exit.
        return self._stack.__exit__(exc_type, exc, traceback)
