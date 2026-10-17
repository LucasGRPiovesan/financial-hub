"""Payload schemas of business events: a small, bounded subset of JSON Schema (draft 2020-12).

The hub does not depend on a JSON Schema engine (no ``jsonschema`` dependency); a manifest
declares each business event's payload with the keywords below and the hub checks payloads
itself, in time bounded by the schema size and the payload size:

``type`` (``object`` | ``array`` | ``string`` | ``integer`` | ``boolean`` | ``null``), ``title``,
``description``, ``properties``, ``required``, ``additionalProperties`` (boolean), ``items``,
``minItems``, ``maxItems``, ``minLength``, ``maxLength``, ``minimum``, ``maximum`` (integers),
``enum`` and ``format`` (``decimal`` for decimal strings, ``date-time`` for RFC 3339 instants).

There is no ``number`` (floats are refused anywhere in a payload: amounts and quantities are
integers or decimal strings), no ``pattern`` (no regular expression runs over input) and no
``$ref`` (no recursion). A schema nests at most ``MAX_SCHEMA_DEPTH`` levels and has at most
``MAX_SCHEMA_NODES`` nodes.
"""

from datetime import datetime
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from financial_hub.modules.integrations.domain.expressions import EvaluationError, as_decimal
from financial_hub.shared.errors import FieldError

MAX_SCHEMA_DEPTH: Final = 6
MAX_SCHEMA_NODES: Final = 128
MAX_PROPERTIES: Final = 64
MAX_ENUM: Final = 64
MAX_PAYLOAD_DEPTH: Final = 32
MAX_ERRORS: Final = 20
_MAX_BOUND: Final = 10**18 - 1
_MAX_SIZE: Final = 100_000
_MAX_DATETIME_TEXT: Final = 64

PropertyName = Annotated[str, Field(min_length=1, max_length=100)]
EnumValue = Annotated[str, Field(max_length=256)] | int | bool | None
Size = Annotated[int, Field(ge=0, le=_MAX_SIZE)]
Bound = Annotated[int, Field(ge=-_MAX_BOUND, le=_MAX_BOUND)]

_KEYWORDS_BY_TYPE: Final = {
    "object": {"properties", "required", "additional_properties"},
    "array": {"items", "min_items", "max_items"},
    "string": {"min_length", "max_length", "format", "enum"},
    "integer": {"minimum", "maximum", "enum"},
    "boolean": {"enum"},
    "null": set[str](),
}
_TYPED_KEYWORDS: Final[set[str]] = set[str]().union(*_KEYWORDS_BY_TYPE.values())


class PayloadSchema(BaseModel):
    """One node of a payload schema (the root must be an ``object``)."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, populate_by_name=True, serialize_by_alias=True
    )

    type: Literal["object", "array", "string", "integer", "boolean", "null"]
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    properties: dict[PropertyName, PayloadSchema] | None = Field(
        default=None, max_length=MAX_PROPERTIES
    )
    required: list[PropertyName] | None = Field(default=None, max_length=MAX_PROPERTIES)
    additional_properties: bool | None = Field(default=None, alias="additionalProperties")
    items: PayloadSchema | None = None
    min_items: Size | None = Field(default=None, alias="minItems")
    max_items: Size | None = Field(default=None, alias="maxItems")
    min_length: Size | None = Field(default=None, alias="minLength")
    max_length: Size | None = Field(default=None, alias="maxLength")
    minimum: Bound | None = None
    maximum: Bound | None = None
    enum: list[EnumValue] | None = Field(default=None, min_length=1, max_length=MAX_ENUM)
    format: Literal["decimal", "date-time"] | None = None

    @model_validator(mode="after")
    def _keywords_fit_the_type(self) -> Self:
        allowed = _KEYWORDS_BY_TYPE[self.type]
        misplaced = sorted(
            name for name in _TYPED_KEYWORDS - allowed if getattr(self, name) is not None
        )
        if misplaced:
            raise ValueError(f"{', '.join(misplaced)} do not apply to type {self.type}")
        if self.required is not None:
            declared = set(self.properties or {})
            unknown = sorted(set(self.required) - declared)
            if unknown:
                raise ValueError(f"required names undeclared properties: {', '.join(unknown)}")
        for low, high, label in (
            (self.min_items, self.max_items, "minItems/maxItems"),
            (self.min_length, self.max_length, "minLength/maxLength"),
            (self.minimum, self.maximum, "minimum/maximum"),
        ):
            if low is not None and high is not None and low > high:
                raise ValueError(f"{label}: the lower bound exceeds the upper bound")
        if self.enum is not None and self.type in {"string", "integer", "boolean"}:
            for value in self.enum:
                if not _has_type(value, self.type):
                    raise ValueError(f"enum values must be of type {self.type}")
        return self


def check_schema_bounds(schema: PayloadSchema) -> None:
    """``ValueError`` unless the root is an object and the tree is within the size bounds."""
    if schema.type != "object":
        raise ValueError("a payload schema describes an object")
    nodes = 0
    pending: list[tuple[PayloadSchema, int]] = [(schema, 1)]
    while pending:
        node, depth = pending.pop()
        nodes += 1
        if depth > MAX_SCHEMA_DEPTH:
            raise ValueError(f"payload schemas nest at most {MAX_SCHEMA_DEPTH} levels")
        if nodes > MAX_SCHEMA_NODES:
            raise ValueError(f"payload schemas have at most {MAX_SCHEMA_NODES} nodes")
        pending.extend((child, depth + 1) for child in (node.properties or {}).values())
        if node.items is not None:
            pending.append((node.items, depth + 1))


def _in_enum(value: object, options: list[EnumValue]) -> bool:
    """Equal to an option of the same JSON type (``True`` is not ``1``)."""
    return any(type(value) is type(option) and value == option for option in options)


def _has_type(value: object, kind: str) -> bool:
    match kind:
        case "object":
            return isinstance(value, dict)
        case "array":
            return isinstance(value, list)
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case _:
            return value is None


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def find_float(document: Any) -> str | None:
    """The pointer of the first float in ``document`` (iterative, depth-bounded), or ``None``.

    ``ValueError`` when the document nests deeper than ``MAX_PAYLOAD_DEPTH``.
    """
    pending: list[tuple[Any, str, int]] = [(document, "", 0)]
    while pending:
        value, pointer, depth = pending.pop()
        if depth > MAX_PAYLOAD_DEPTH:
            raise ValueError(f"payloads nest at most {MAX_PAYLOAD_DEPTH} levels")
        if isinstance(value, float):
            return pointer
        if isinstance(value, dict):
            pending.extend(
                (child, f"{pointer}/{_escape(str(key))}", depth + 1)  # pyright: ignore[reportUnknownArgumentType]
                for key, child in value.items()  # pyright: ignore[reportUnknownVariableType]
            )
        elif isinstance(value, list):
            pending.extend(
                (child, f"{pointer}/{index}", depth + 1)
                for index, child in enumerate(value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
            )
    return None


def validate_payload(schema: PayloadSchema, payload: Any, base: str = "") -> list[FieldError]:
    """The ways ``payload`` breaks ``schema`` (at most ``MAX_ERRORS``); empty when it conforms.

    Pointers are prefixed with ``base`` (e.g. ``/payload``). Recursion follows the *schema*, whose
    depth is bounded, never the payload.
    """
    errors: list[FieldError] = []
    _check(schema, payload, base, errors)
    return errors[:MAX_ERRORS]


def _check(schema: PayloadSchema, value: Any, pointer: str, errors: list[FieldError]) -> None:
    if len(errors) >= MAX_ERRORS:
        return
    if not _has_type(value, schema.type):
        errors.append(FieldError(pointer, f"must be of type {schema.type}", "type"))
        return
    if schema.enum is not None and not _in_enum(value, schema.enum):
        errors.append(FieldError(pointer, "is not one of the allowed values", "enum"))
    match schema.type:
        case "string":
            _check_string(schema, value, pointer, errors)
        case "integer":
            if schema.minimum is not None and value < schema.minimum:
                errors.append(FieldError(pointer, f"must be >= {schema.minimum}", "minimum"))
            if schema.maximum is not None and value > schema.maximum:
                errors.append(FieldError(pointer, f"must be <= {schema.maximum}", "maximum"))
        case "array":
            if schema.min_items is not None and len(value) < schema.min_items:
                errors.append(
                    FieldError(pointer, f"needs >= {schema.min_items} items", "min_items")
                )
            if schema.max_items is not None and len(value) > schema.max_items:
                errors.append(
                    FieldError(pointer, f"allows <= {schema.max_items} items", "max_items")
                )
            if schema.items is not None:
                for index, item in enumerate(value):
                    if len(errors) >= MAX_ERRORS:
                        return
                    _check(schema.items, item, f"{pointer}/{index}", errors)
        case "object":
            _check_object(schema, value, pointer, errors)
        case _:
            pass


def _check_string(
    schema: PayloadSchema, value: str, pointer: str, errors: list[FieldError]
) -> None:
    if schema.min_length is not None and len(value) < schema.min_length:
        errors.append(FieldError(pointer, f"needs >= {schema.min_length} characters", "min_length"))
    if schema.max_length is not None and len(value) > schema.max_length:
        errors.append(
            FieldError(pointer, f"allows <= {schema.max_length} characters", "max_length")
        )
    if schema.format == "decimal":
        try:
            as_decimal(value)
        except EvaluationError as error:
            errors.append(FieldError(pointer, str(error), "format"))
    elif schema.format == "date-time" and not _is_instant(value):
        errors.append(FieldError(pointer, "must be an RFC 3339 date-time with offset", "format"))


def _is_instant(value: str) -> bool:
    if len(value) > _MAX_DATETIME_TEXT:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _check_object(
    schema: PayloadSchema, value: dict[str, Any], pointer: str, errors: list[FieldError]
) -> None:
    properties = schema.properties or {}
    for name in schema.required or []:
        if name not in value:
            errors.append(FieldError(f"{pointer}/{_escape(name)}", "is required", "required"))
    for name, child in value.items():
        if len(errors) >= MAX_ERRORS:
            return
        child_pointer = f"{pointer}/{_escape(name)}"
        if name in properties:
            _check(properties[name], child, child_pointer, errors)
        elif schema.additional_properties is False:
            errors.append(FieldError(child_pointer, "is not allowed", "additional_properties"))


PayloadSchema.model_rebuild()
