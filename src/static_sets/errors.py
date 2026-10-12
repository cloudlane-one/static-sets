"""Exceptions raised by the StaticSet operator library.

Every error carries the reason a definition, a translation or an installation
step was refused, so that the controller can surface it as a condition instead of
failing silently.
"""

from __future__ import annotations

__all__ = [
    "CleanupError",
    "DefinitionError",
    "InstallationError",
    "NotOfflineReadyError",
    "OwnershipError",
    "PathSecurityError",
    "SecretDisclosureError",
    "StaticSetsError",
    "TranslationError",
    "UnsupportedFieldError",
]


class StaticSetsError(Exception):
    """Base class for every error raised by this library."""


class DefinitionError(StaticSetsError):
    """A StaticSet definition is internally inconsistent or unserviceable."""


class TranslationError(StaticSetsError):
    """A workload template cannot be translated into a self-contained static Pod."""


class UnsupportedFieldError(TranslationError):
    """A template uses a field this operator deliberately does not support.

    The message names the field and, where one exists, the supported
    alternative, so that the rejection is actionable rather than merely
    restrictive.
    """


class SecretDisclosureError(TranslationError):
    """A template would place Secret material somewhere it must never appear.

    Raised when Secret data would have to be written into the static Pod manifest
    (and therefore into the mirror Pod and any status field that echoes it) while
    the StaticSet has not opted into that disclosure.
    """


class NotOfflineReadyError(StaticSetsError):
    """A revision must not be declared offline-ready yet.

    Raised when image preparation is required by policy but cannot be confirmed,
    so the installer refuses to publish a revision that would not survive a host
    reboot without registry access.
    """


class InstallationError(StaticSetsError):
    """A static workload could not be installed durably on a node."""


class PathSecurityError(InstallationError):
    """A path involved in an installation failed validation.

    Raised for traversal, absolute-path, symlink and ownership violations: the
    installer only ever touches files it owns beneath directories it was told
    about.
    """


class OwnershipError(InstallationError):
    """An ownership record does not authorize the requested local mutation.

    Raised when an installer or cleanup worker would act on a placement record
    that belongs to a different static workload, a different node or a newer
    generation.
    """


class CleanupError(StaticSetsError):
    """A static workload could not be cleaned up.

    Raised when the cleanup protocol cannot proceed, for example because the
    node holding the workload is unreachable and the abandonment policy does not
    permit force-deleting the record.
    """
