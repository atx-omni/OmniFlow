"""Narrow, case-sensitive correspondence for Omni authored file names.

Native Git exports may add one YAML serialization suffix to the API file name.
This is not path sanitization: callers must validate paths and retain original
Git names for blob reads, snapshot digests, and source locations.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath

_AUTHORED_SUFFIXES = (".view", ".topic", ".composite_topic", ".relationships")
SEMANTIC_SUFFIXES = frozenset((*_AUTHORED_SUFFIXES, ".yaml", ".yml"))


def is_semantic_file(path: str) -> bool:
    """Accept authored API/snapshot formats, including API-confirmed custom YAML."""
    value = PurePosixPath(path)
    return value.name in {"model", "relationships"} or value.suffix in SEMANTIC_SUFFIXES


def is_git_model_file(path: str) -> bool:
    """Recognize model-relative Omni sources, not arbitrary repository YAML.

    Keep native API names and supported Git serialization wrappers equivalent.
    Directories organize files; identity is scoped to the registered model.
    Custom YAML names require API evidence once a model is explicitly selected.
    """
    identity = filename_identity(path)
    return identity in {"model", "relationships"} or identity.endswith(_AUTHORED_SUFFIXES)


def is_repo_control_file(path: str) -> bool:
    """Repository-relative control files have their own validation, not model checks."""
    return path in {".omniflow.yml", ".omniflow.example.yml"} or path.startswith(
        (".omni/", ".github/", ".omniflow/")
    )


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


def filename_identity(path: str) -> str:
    """Case-sensitive model-local filename, with one supported export wrapper.

    Callers must first enforce the registered model scope and validate paths.
    This is not a repository-wide lookup or permission to ignore file contents.
    """
    return authored_file_identity(PurePosixPath(path).name)


def filename_index(paths: Iterable[str]) -> dict[str, str]:
    """Map unique model-local filenames to their original, unmodified paths."""
    result: dict[str, str] = {}
    for path in paths:
        identity = filename_identity(path)
        if identity in result:
            raise AmbiguousModelPath("Ambiguous authored filename identity")
        result[identity] = path
    return result
