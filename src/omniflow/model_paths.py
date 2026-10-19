"""Narrow, case-sensitive correspondence for Omni authored file names.

Native Git exports may add one YAML serialization suffix to the API file name.
This is not path sanitization: callers must validate paths and retain original
Git names for blob reads, snapshot digests, and source locations.
"""

from __future__ import annotations

from collections.abc import Iterable

_AUTHORED_SUFFIXES = (".view", ".topic", ".composite_topic", ".relationships")


class AmbiguousModelPath(ValueError):
    """More than one source file represents the same authored identity."""


def authored_file_identity(path: str) -> str:
    """Remove exactly one known serialization wrapper, never directories."""
    for wrapper in (".yaml", ".yml"):
        if path.endswith(wrapper):
            stem = path[:-len(wrapper)]
            if stem in {"model", "relationships"} or stem.endswith(_AUTHORED_SUFFIXES):
                return stem
    return path


def identity_index(paths: Iterable[str]) -> dict[str, str]:
    """Map authored identities to original paths; ambiguous sets fail closed."""
    result: dict[str, str] = {}
    for path in paths:
        identity = authored_file_identity(path)
        if identity in result:
            # Do not include potentially sensitive source paths in exceptions.
            raise AmbiguousModelPath("Ambiguous authored file identity")
        result[identity] = path
    return result
