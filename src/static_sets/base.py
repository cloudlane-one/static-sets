"""Shared Pydantic base for every model that is (de)serialized to Kubernetes.

All wire-facing models alias their fields to camelCase through a single alias
generator, which keeps the Python API snake_case while the CRD schema, the
manifest files and the API server all see Kubernetes' own field names. A single
``alias_generator`` sets both the validation and the serialization alias, which
is what ``based_operators`` requires of a custom-resource model.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

__all__ = ["WireModel"]


class WireModel(BaseModel):
    """A model whose wire representation uses Kubernetes' camelCase field names.

    Unknown fields are forbidden: the operator supports a conservative,
    validated subset of the workload API, and a field it does not understand must
    be rejected rather than quietly ignored.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )
