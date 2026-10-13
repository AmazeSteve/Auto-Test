"""Atomic, sensitive local recovery material for the C1 acceptance harness."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from wizbank_api_test.services.user_update_payload_builder import WRITE_MODEL_FIELDS


BASE_COMMIT = "1eacd8c98184ff3b0f786054dde09a27910a196c"
DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / ".runtime" / "recovery"


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256(value):
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if ("cookie" in normalized or "password" in normalized
                    or normalized in {"authorization", "usr_pwd", "pwd", "token", "secret", "api_key"}
                    or normalized.endswith("_token")):
                raise ValueError("C1 recovery material must not contain credential fields")
            _reject_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_credentials(item)


def validate_envelope(envelope):
    if not isinstance(envelope, dict) or set(envelope) != {"target", "payload"} or envelope["target"] != "json":
        raise ValueError("C1 recovery envelope must contain only target=json and payload")
    payload = envelope["payload"]
    if not isinstance(payload, dict) or set(payload) != set(WRITE_MODEL_FIELDS):
        raise ValueError("C1 recovery payload must contain exactly the frozen 27 fields")
    _reject_credentials(envelope)


class PendingRecoveryError(ValueError):
    def __init__(self, count, directory):
        self.count = count
        self.directory = directory
        super().__init__(f"C1 pending recovery journals: count={count}, directory={directory}")


class RecoveryJournal:
    def __init__(self, directory=DEFAULT_DIRECTORY):
        self.directory = Path(directory).resolve()
        root = DEFAULT_DIRECTORY.parent.parent
        if self.directory.is_relative_to(root) and not self.directory.is_relative_to(root / ".runtime"):
            raise ValueError("C1 recovery material inside the checkout must stay under ignored .runtime")
        self.path = None
        self.status = "not_created"

    def pending_records(self):
        """Inspect file names/metadata only; never open recovery payloads."""
        try:
            entries = list(self.directory.iterdir())
        except FileNotFoundError:
            return ()
        return tuple(sorted(path for path in entries if path.suffix.lower() == ".json" and path.is_file()))

    def has_pending(self):
        return bool(self.pending_records())

    def assert_no_pending(self):
        pending = self.pending_records()
        if pending:
            raise PendingRecoveryError(len(pending), self.directory)

    def create(self, case_id, target_username, restore_payload):
        validate_envelope(restore_payload)
        if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", case_id):
            raise ValueError("C1 journal requires a safe Case ID")
        if self.path is not None:
            raise ValueError("C1 journal already exists for this invocation")
        self.assert_no_pending()
        record = {
            "schema_version": 1, "status": "prepared", "case_id": case_id,
            "created_at": datetime.now(timezone.utc).isoformat(), "base_commit": BASE_COMMIT,
            "target_user_hash": sha256(target_username),
            "restore_payload_sha256": sha256(restore_payload),
            "restore_payload": deepcopy(restore_payload),
        }
        contents = stable_json(record)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.directory / f"{case_id}-{uuid4().hex}.json"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".journal-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            self.path = destination
            self.status = "prepared"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination

    def finish(self, *, success):
        if self.path is not None:
            if success:
                self.path.unlink()
                self.status = "deleted"
            else:
                self.status = "retained"

    def summary(self):
        return {"path": str(self.path) if self.path else None, "status": self.status}
