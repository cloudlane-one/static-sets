"""Strict admission validation for a StaticSet.

The generated CustomResourceDefinition is a *structural* schema, so the API server
prunes a field it does not know instead of refusing it. A pruned field means a
template that asked for something the operator cannot compile would be accepted and
then silently ignored — the one outcome the translation layer exists to prevent.

This module turns the same model validation into a list of actionable problems, so
the operator can refuse such an object at admission instead of pruning it. It is
wired to a validating admission webhook in the operator entry point; without that
webhook the CRD-level pruning still applies, which is why the installation guide
tells you to deploy with ``--webhook``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from static_sets.errors import StaticSetsError
from static_sets.models import StaticSet
from static_sets.translation import check_policies

__all__ = ["strict_problems"]

_MAX_PROBLEMS = 20
"""How many problems to report before summarizing the rest."""


def strict_problems(body: Mapping[str, Any]) -> list[str]:
    """Return the reasons a StaticSet body must be refused.

    Every problem names the exact field path, so the message can be attached to the
    rejection the user sees from ``kubectl``.

    Args:
        body: The raw object as the API server received it.

    Returns:
        The problems, in field order. An empty list means the object is acceptable.
    """
    try:
        resource = StaticSet.model_validate(body)
    except ValidationError as error:
        return _describe(error)
    try:
        check_policies(resource.spec)
    except StaticSetsError as error:
        return [f"spec: {error}"]
    return []


def _describe(error: ValidationError) -> list[str]:
    """Turn Pydantic's error report into stable, readable problems.

    Args:
        error: The validation failure.

    Returns:
        One message per problem, truncated to a bounded number.
    """
    problems: list[str] = []
    for item in error.errors()[: _MAX_PROBLEMS + 1]:
        location = ".".join(str(part) for part in item["loc"])
        problems.append(f"{location or '<root>'}: {item['msg']}")
    if len(problems) > _MAX_PROBLEMS:
        extra = len(error.errors()) - _MAX_PROBLEMS
        problems = problems[:_MAX_PROBLEMS]
        problems.append(f"... and {extra} further problem(s)")
    return problems
