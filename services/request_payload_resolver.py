"""Resolve a request plan from an Effect Before response, without HTTP/effects."""

from copy import deepcopy
from dataclasses import dataclass

from jsonpath_ng import parse

from wizbank_api_test.core.variable_resolver import VariableResolver
from wizbank_api_test.services.builder_registry import BuilderRegistry


@dataclass(frozen=True)
class ResolvedRequestPayload:
    target: str
    body: dict
    restore_payload: dict
    changed_fields: frozenset[str]
    builder_name: str


class RequestPayloadResolver:
    @staticmethod
    def validate_config(config):
        """Shared structural checks; no variable resolution or runtime Context."""
        if not isinstance(config, dict):
            raise ValueError("request_builder must be a dict")
        allowed = {"name", "source", "arguments", "mutable_fields", "overrides"}
        if set(config) - allowed:
            raise ValueError("request_builder contains unknown keys")
        registration = BuilderRegistry.get(config.get("name"))
        source = config.get("source")
        if not isinstance(source, dict) or set(source) != {"type", "expression"}:
            raise ValueError("request_builder.source requires type and expression")
        if source["type"] != "effect_before":
            raise ValueError("request_builder.source.type must be effect_before")
        expression = source["expression"]
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("request_builder.source.expression must be a JSONPath string")
        try:
            path = parse(expression)
        except Exception as exc:
            raise ValueError("request_builder.source.expression is invalid JSONPath") from exc
        arguments = config.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("request_builder.arguments must be a dict")
        for key in registration.required_text_arguments:
            value = arguments.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"request_builder.arguments.{key} must be nonblank")
        if set(arguments) - set(registration.required_text_arguments):
            raise ValueError("request_builder.arguments contains unsupported keys")
        fields = config.get("mutable_fields")
        if (not isinstance(fields, list) or not fields
                or not all(isinstance(field, str) and field.strip() for field in fields)
                or len(set(fields)) != len(fields)):
            raise ValueError("request_builder.mutable_fields must be a nonempty unique string list")
        if not set(fields) <= registration.mutable_fields:
            raise ValueError("request_builder.mutable_fields contains unsupported fields")
        overrides = config.get("overrides")
        if not isinstance(overrides, dict) or set(overrides) != set(fields):
            raise ValueError("request_builder.overrides keys must equal mutable_fields")
        return registration, path

    @classmethod
    def resolve(cls, case: dict, effect_before) -> ResolvedRequestPayload:
        config = VariableResolver.resolve(deepcopy(case.get("request_builder")))
        registration, path = cls.validate_config(config)
        matches = path.find(effect_before)
        if len(matches) != 1:
            raise ValueError("request_builder.source.expression must match exactly one value")
        plan = registration.build(
            deepcopy(matches[0].value),
            **config["arguments"],
            mutable_fields=set(config["mutable_fields"]),
            overrides=config["overrides"],
        )
        return ResolvedRequestPayload(
            target=registration.target,
            body=deepcopy(plan.mutation_payload),
            restore_payload={
                "target": registration.target,
                "payload": deepcopy(plan.baseline_payload),
            },
            changed_fields=plan.changed_fields,
            builder_name=config["name"],
        )
