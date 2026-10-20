"""Conservative, typed correspondence inside one registered Omni model.

The caller owns path/scope validation, inventory bounds and exact byte checks.
Identity is not content equality: this module never pairs files by their bytes.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml
from yaml.nodes import MappingNode, ScalarNode

from .exceptions import ConfigError
from .model_paths import filename_identity
from .yaml_security import parse_secure_yaml


@dataclass(frozen=True)
class ArtifactMatch:
    pairs: dict[str, str]
    unmatched_git: set[str]
    unmatched_omni: set[str]
    pairing_methods: dict[str, str]


class ArtifactIdentityError(ValueError):
    """Paths are restricted structured evidence, never public exception text."""

    reason = "ambiguous_file_identity"

    def __init__(self, identity_reason: str, *, git_paths=(), omni_paths=()) -> None:
        super().__init__("Omni artifact identity could not be established unambiguously.")
        self.identity_reason = identity_reason
        candidates = [
            {"git_path": path, "omni_path": None, "side": "git"} for path in sorted(set(git_paths))
        ] + [
            {"git_path": None, "omni_path": path, "side": "omni"} for path in sorted(set(omni_paths))
        ]
        self.candidate_count = len(candidates)
        self.candidates = candidates[:5]


def identity_digest(pairs: dict[str, str], *, omni_view_names: dict[str, str] | None = None) -> str:
    """Bind a proof to exact correspondence and canonical metadata, not bytes alone.

    Inputs come from validated resolution/metadata. Structured pairs preserve
    namespace boundaries; missing metadata differs from a present empty map.
    """
    evidence = {
        "version": 1,
        "pairs": sorted(pairs.items()),
        "view_names": None if omni_view_names is None else sorted(omni_view_names.items()),
    }
    encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _kind(name: str) -> str:
    if name.endswith(".view"):
        return "view"
    if name.endswith((".topic", ".composite_topic")):
        return "topic"
    if name.endswith(".relationships"):
        return "typed_relationship"
    if name in {"model", "relationships"}:
        return "singleton"
    return "generic"


def _group(files: dict[str, str], key) -> dict[object, list[str]]:
    groups: dict[object, list[str]] = defaultdict(list)
    for path in sorted(files):
        groups[key(path)].append(path)
    return dict(groups)


def _authored_path(path: str) -> str:
    return str(PurePosixPath(path).parent / filename_identity(path))


def _validate_inventory(files: dict[str, str], side: str) -> None:
    for paths in _group(files, _authored_path).values():
        if len(paths) > 1:
            raise ArtifactIdentityError("duplicate_authored_path", **{side + "_paths": paths})
    for (kind, _), paths in _group(files, lambda path: (_kind(filename_identity(path)), filename_identity(path))).items():
        if len(paths) > 1 and kind in {"singleton", "topic", "typed_relationship"}:
            raise ArtifactIdentityError("duplicate_" + kind, **{side + "_paths": paths})


def _canonical_stem(path: str) -> str:
    name = filename_identity(path)
    for suffix in (".query.view", ".view"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _valid_component(value: object) -> bool:
    return (
        isinstance(value, str) and bool(value) and value == value.strip() and len(value) <= 1024
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _physical_identity(path: str, text: str) -> tuple[str, str | None, str, str] | None:
    """A missing catalog is its own default-catalog namespace, not a wildcard."""
    try:
        payload = parse_secure_yaml(text, source=path)
    except ConfigError:
        # Syntax errors belong to model validation when another identity works.
        return None
    if not isinstance(payload, dict) or "sql" in payload or "query" in payload:
        return None
    if not _valid_component(payload.get("schema")) or not _valid_component(payload.get("table_name")):
        return None
    catalog = payload.get("catalog")
    if "catalog" in payload and not _valid_component(catalog):
        return None
    # Do not derive identity from YAML's last-key-wins or merge-key behavior.
    # Secure parsing above has already enforced graph, alias and resource bounds.
    document = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(document, MappingNode):
        return None
    keys = [key.value for key, _ in document.value if isinstance(key, ScalarNode)]
    if "<<" in keys or any(keys.count(key) > 1 for key in ("catalog", "schema", "table_name", "sql", "query")):
        return None
    return filename_identity(path), catalog, payload["schema"], payload["table_name"]


def match_artifacts(
    git_files: dict[str, str], omni_files: dict[str, str], *, omni_view_names: dict[str, str] | None = None,
) -> ArtifactMatch:
    """Match complete model-relative inventories without weakening content proof.

    Supplied view names are canonical-name -> API-path, already normalized by the
    API ingestion boundary. All fallback uniqueness checks use full inventories,
    never just files remaining after an earlier match.
    """
    _validate_inventory(git_files, "git")
    _validate_inventory(omni_files, "omni")
    canonical = {} if omni_view_names is None else omni_view_names
    if (
        not isinstance(canonical, dict)
        or any(not _valid_component(name) or not isinstance(path, str) or path not in omni_files
               or _kind(filename_identity(path)) not in {"view", "generic"} for name, path in canonical.items())
        or len(set(canonical.values())) != len(canonical)
    ):
        raise ArtifactIdentityError("invalid_canonical_view_map")

    pairs: dict[str, str] = {}
    methods: dict[str, str] = {}
    api_paths = {_authored_path(path): path for path in omni_files}
    for path in sorted(git_files):
        if _authored_path(path) in api_paths:
            pairs[path] = api_paths[_authored_path(path)]
            methods[path] = "authored_path"

    git_names = _group(git_files, filename_identity)
    omni_names = _group(omni_files, filename_identity)
    git_view_stems = _group(
        {path: text for path, text in git_files.items() if _kind(filename_identity(path)) == "view"},
        _canonical_stem,
    )
    # Parse only duplicate view-name groups that actually need qualification.
    qualified_git: dict[object, list[str]] = defaultdict(list)
    qualified_omni: dict[object, list[str]] = defaultdict(list)
    git_physical: dict[str, object] = {}
    incomplete_qualified_names: set[str] = set()
    ambiguous_names = {
        name for name in git_names.keys() & omni_names.keys()
        if _kind(name) == "view" and (len(git_names[name]) > 1 or len(omni_names[name]) > 1)
        and any(path not in pairs for path in git_names[name])
    }
    for files, names, qualified in ((git_files, git_names, qualified_git), (omni_files, omni_names, qualified_omni)):
        for name in ambiguous_names:
            for path in names[name]:
                identity = _physical_identity(path, files[path])
                if identity is not None:
                    qualified[identity].append(path)
                    if files is git_files:
                        git_physical[path] = identity
                else:
                    # An unreadable sibling might claim the same qualification.
                    # Do not mistake absent evidence for global uniqueness.
                    incomplete_qualified_names.add(name)

    proposals: dict[str, dict[str, str]] = {}
    for path in sorted(set(git_files) - pairs.keys()):
        name = filename_identity(path)
        kind = _kind(name)
        choices: dict[str, str] = {}
        if len(git_names[name]) == 1 and len(omni_names.get(name, [])) == 1:
            method = {
                "singleton": "singleton", "topic": "topic_name", "typed_relationship": "typed_relationship_name",
                "view": "unique_view_filename", "generic": "unique_generic_filename",
            }[kind]
            choices[omni_names[name][0]] = method
        if kind == "view":
            stem = _canonical_stem(path)
            # API ingestion may confirm a generic YAML file is a view. Preserve
            # that metadata, but keep such files on exact/unique-name matching;
            # do not invent a typed Git alias from its semantic name.
            if (
                len(git_view_stems[stem]) == 1 and stem in canonical
                and _kind(filename_identity(canonical[stem])) == "view"
            ):
                choices[canonical[stem]] = "canonical_view_name"
            identity = git_physical.get(path)
            if (
                name not in incomplete_qualified_names and identity is not None
                and len(qualified_git[identity]) == 1 and len(qualified_omni.get(identity, [])) == 1
            ):
                choices[qualified_omni[identity][0]] = "qualified_view"
        if choices:
            proposals[path] = choices

    targets: dict[str, list[str]] = defaultdict(list)
    for path, choices in proposals.items():
        if len(choices) != 1:
            raise ArtifactIdentityError("conflicting_view_correspondence", git_paths=[path], omni_paths=choices)
        target = next(iter(choices))
        targets[target].append(path)
    used_targets = set(pairs.values())
    for target, paths in targets.items():
        if len(paths) > 1 or target in used_targets:
            raise ArtifactIdentityError("ambiguous_view_correspondence", git_paths=paths, omni_paths=[target])
        path = paths[0]
        pairs[path] = target
        used_targets.add(target)
        methods[path] = proposals[path][target]

    unmatched_git, unmatched_omni = set(git_files) - pairs.keys(), set(omni_files) - set(pairs.values())
    for name in git_names.keys() & omni_names.keys():
        left, right = set(git_names[name]) & unmatched_git, set(omni_names[name]) & unmatched_omni
        if left and right and (len(git_names[name]) > 1 or len(omni_names[name]) > 1):
            raise ArtifactIdentityError("ambiguous_view_correspondence", git_paths=left, omni_paths=right)
    return ArtifactMatch(pairs, unmatched_git, unmatched_omni, methods)
