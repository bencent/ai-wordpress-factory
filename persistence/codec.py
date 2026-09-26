"""Strict versioned record/snapshot codecs; never import types named by stored JSON."""
import json
import math
import types
from dataclasses import fields
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints


class CodecError(ValueError):
    """Invalid or unsupported stored data (payload deliberately omitted)."""


def normalize(value):
    if isinstance(value, Enum):
        return normalize(value.value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [normalize(v) for v in value]
    if type(value) is dict and all(type(k) is str for k in value):
        return {k: normalize(v) for k, v in value.items()}
    raise CodecError("Unsupported JSON value")


def convert(value, hint):
    if hint is Any:
        return normalize(value)
    origin, args = get_origin(hint), get_args(hint)
    if origin is types.UnionType:
        for choice in args:
            try:
                return convert(value, choice)
            except CodecError:
                pass
        raise CodecError("Invalid optional value")
    if hint is type(None) and value is None:
        return None
    if origin is dict and type(value) is dict:
        return {convert(k, args[0]): convert(v, args[1]) for k, v in value.items()}
    if origin is list and type(value) is list:
        return [convert(v, args[0]) for v in value]
    if isinstance(hint, type) and issubclass(hint, Enum):
        try:
            return hint(value)
        except (ValueError, TypeError):
            raise CodecError("Unknown enum value") from None
    if hint in (str, int, bool, float) and type(value) is hint:
        return value
    raise CodecError("Invalid field type")


def record_from_mapping(cls, data):
    names = {f.name for f in fields(cls)}
    if type(data) is not dict or set(data) != names:
        raise CodecError("Missing or unknown record field")
    hints = get_type_hints(cls)
    return cls(**{key: convert(value, hints[key]) for key, value in data.items()})


def record_to_mapping(record):
    data = {f.name: normalize(getattr(record, f.name)) for f in fields(record)}
    record_from_mapping(type(record), data)
    return data


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CodecError("Duplicate JSON field")
        result[key] = value
    return result


def encode_snapshot(value):
    return json.dumps({"schema_version": 1, "data": normalize(value)},
                      ensure_ascii=False, allow_nan=False, sort_keys=True)


def decode_snapshot(raw):
    try:
        envelope = json.loads(raw, object_pairs_hook=_pairs)
        if (type(envelope) is not dict or set(envelope) != {"schema_version", "data"}
                or type(envelope["schema_version"]) is not int
                or envelope["schema_version"] != 1):
            raise CodecError("Unsupported snapshot envelope")
        return normalize(envelope["data"])
    except (ValueError, TypeError, RecursionError):
        raise CodecError("Invalid or unsupported snapshot") from None


def encode_record(record):
    return encode_snapshot(record_to_mapping(record))


def decode_record(cls, raw):
    return record_from_mapping(cls, decode_snapshot(raw))
