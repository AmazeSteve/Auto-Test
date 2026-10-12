"""Minimal routing metadata for the frozen B1 payload builder."""

from dataclasses import dataclass
from typing import Callable

from wizbank_api_test.services.user_update_payload_builder import (
    SUPPORTED_MUTABLE_FIELDS,
    UserUpdatePayloadBuilder,
)


@dataclass(frozen=True)
class BuilderRegistration:
    build: Callable
    target: str
    mutable_fields: frozenset[str]
    required_text_arguments: tuple[str, ...]


class BuilderRegistry:
    _builders = {
        "user_update": BuilderRegistration(
            UserUpdatePayloadBuilder.build, "json", SUPPORTED_MUTABLE_FIELDS,
            ("cur_lan",),
        ),
    }

    @classmethod
    def get(cls, name: str) -> BuilderRegistration:
        if not isinstance(name, str) or name not in cls._builders:
            raise ValueError("request_builder.name must be a registered builder")
        return cls._builders[name]
