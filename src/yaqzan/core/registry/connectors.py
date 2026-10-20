"""Reading one connector folder: its reviewed ``plugin.yaml`` (Rulebook §3.2, ADR-027).

Yaqzan decides what a connector may do from *our* manifest, never from what
an outside server says about itself (the MCP spec calls those hints
untrusted, research/07 §3.3). A manifest that declares no permissions counts
as ``act``, so it could only ever propose. A connector that declares itself
read-only may list only ``GET`` endpoints. The ``interface`` block is the
only place its display name and category live (white-label, ADR-027).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from yaqzan.core.registry.files import ItemError, fingerprint, load_mapping, read_text, refuse_secrets
from yaqzan.core.registry.models import ITEM_ID, Connector
from yaqzan.core.sdk import DataClass, ToolLevel

MANIFEST = "plugin.yaml"
_METHODS = ("GET /", "POST /", "PUT /", "PATCH /", "DELETE /")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Credential(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    scope_hint: str = Field(min_length=1, max_length=300)
    verify: bool = True


class _Interface(_Strict):
    display_name: str = Field(min_length=1, max_length=40)
    category: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=200)


class _Manifest(_Strict):
    id: str = Field(pattern=ITEM_ID, max_length=64)
    type: Literal["connector"]
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    sdk: str = Field(min_length=1, max_length=40)
    permissions: tuple[ToolLevel, ...] = (ToolLevel.ACT,)
    egress: tuple[str, ...] = Field(min_length=1)
    endpoints: tuple[str, ...] = Field(min_length=1)
    credentials: tuple[_Credential, ...] = ()
    data_class: DataClass
    interface: _Interface

    @model_validator(mode="after")
    def _read_only_means_get_only(self) -> _Manifest:
        if not self.permissions:
            raise ValueError("permissions may not be empty (leave the field out to mean 'act')")
        if not all(endpoint.startswith(_METHODS) for endpoint in self.endpoints):
            raise ValueError("each endpoint must be a method and a path, e.g. 'GET /items'")
        if self.level is ToolLevel.READ and not all(e.startswith("GET /") for e in self.endpoints):
            raise ValueError("it declares itself read-only but lists an endpoint that is not GET")
        return self

    @property
    def level(self) -> ToolLevel:
        """The highest permission declared."""
        return max(self.permissions, key=lambda level: level.rank)


def read_connector(folder: Path) -> Connector:
    """The connector in ``folder``, or :class:`ItemError` saying why it was refused."""
    path = folder / MANIFEST
    if not path.is_file():
        raise ItemError("registry.no_manifest", f"the folder has no {MANIFEST}")
    fields = load_mapping(read_text(path), MANIFEST)
    refuse_secrets(fields, MANIFEST)
    try:
        manifest = _Manifest.model_validate(fields)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "the manifest"
        raise ItemError("registry.bad_manifest", f"{MANIFEST}: {where}: {first['msg']}") from None
    if manifest.id != folder.name:
        raise ItemError(
            "registry.name_mismatch", f"the connector is '{manifest.id}' but its folder is '{folder.name}'"
        )
    return Connector(
        id=manifest.id,
        name=manifest.interface.display_name,
        category=manifest.interface.category,
        description=manifest.interface.description,
        version=manifest.version,
        level=manifest.level,
        hosts=manifest.egress,
        endpoints=manifest.endpoints,
        key_slots=tuple(credential.name for credential in manifest.credentials),
        fingerprint=fingerprint(folder),
    )
