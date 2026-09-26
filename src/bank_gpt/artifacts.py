"""Versioned base artifacts and tenant UI bindings."""
from __future__ import annotations

import json
import os
from tempfile import NamedTemporaryFile
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from .models import BaseCapability, TenantBinding


class ArtifactError(ValueError):
    pass


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise ArtifactError("invalid capability ID") from error


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _origin_pair() -> tuple[str, str]:
        """Keep local origins in environment and publish binding URLs with a placeholder."""
        published = os.getenv("BANKGPT_ARTIFACT_PUBLIC_ORIGIN", "").rstrip("/")
        allowed = [item.strip().rstrip("/") for item in
                   os.getenv("BANKGPT_ALLOWED_ORIGINS", "").split(",") if item.strip()]
        if not published or not allowed:
            raise ArtifactError("published bindings require configured allowed and public origins")
        if len(allowed) != 1:
            raise ArtifactError("published bindings require one configured allowed origin")
        return allowed[0], published

    @classmethod
    def _stored_binding(cls, binding: TenantBinding) -> str:
        content = binding.model_dump_json(indent=2)
        pair = cls._origin_pair()
        if binding.allowed_origin.rstrip("/") != pair[0]:
            raise ArtifactError("binding origin must match configured allowed origin")
        content = content.replace(pair[0], pair[1])
        return content + "\n"

    def base_path(self, capability_id: str) -> Path:
        return self.root / f"{_uuid(capability_id)}.json"

    def binding_path(self, capability_id: str, tenant_id: int) -> Path:
        return self.root / f"{_uuid(capability_id)}.tenant-{tenant_id}.json"

    def binding_version_path(self, capability_id: str, tenant_id: int, version: int) -> Path:
        return self.root / f"{_uuid(capability_id)}.tenant-{tenant_id}.v{version}.json"

    def catalog(self, tenant_id: int) -> list[tuple[BaseCapability, TenantBinding]]:
        entries = []
        for path in sorted(self.root.glob(f"*.tenant-{tenant_id}.json")):
            try:
                binding = TenantBinding.model_validate(self._read(path))
                if binding.tenant_id == tenant_id and binding.approval_state == "approved":
                    base = self.base(binding.base_id)
                    binding.resolve(base)
                    entries.append((base, binding))
            except (ArtifactError, ValidationError, ValueError):
                continue
        return entries

    @classmethod
    def _read(cls, path: Path) -> dict:
        try:
            content = path.read_text()
            if ".tenant-" in path.name:
                pair = cls._origin_pair()
                content = content.replace(pair[1], pair[0])
            data = json.loads(content)
        except (OSError, json.JSONDecodeError) as error:
            raise ArtifactError("artifact not found or invalid JSON") from error
        if data.get("schema_version") != "2.0":
            raise ArtifactError(f"unsupported artifact schema {data.get('schema_version')}; rediscover as 2.0")
        return data

    def base(self, capability_id: str) -> BaseCapability:
        try:
            base = BaseCapability.model_validate(self._read(self.base_path(capability_id)))
            if base.id != _uuid(capability_id):
                raise ArtifactError("artifact ID mismatch")
            return base
        except ValidationError as error:
            raise ArtifactError("invalid base artifact") from error

    def binding(self, capability_id: str, tenant_id: int) -> TenantBinding:
        try:
            binding = TenantBinding.model_validate(self._read(self.binding_path(capability_id, tenant_id)))
            if binding.base_id != _uuid(capability_id) or binding.tenant_id != tenant_id:
                raise ArtifactError("binding scope mismatch")
            return binding
        except ValidationError as error:
            raise ArtifactError("invalid tenant binding") from error

    def save_base(self, base: BaseCapability) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.base_path(base.id)
        with path.open("x") as stream:
            stream.write(base.model_dump_json(indent=2) + "\n")

    def save_binding(self, binding: TenantBinding) -> None:
        content = self._stored_binding(binding)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.binding_path(binding.base_id, binding.tenant_id)
        with path.open("x") as stream:
            stream.write(content)
        self.binding_version_path(binding.base_id, binding.tenant_id, binding.version).write_text(
            content)

    def review_binding(self, binding: TenantBinding) -> None:
        path = self.binding_path(binding.base_id, binding.tenant_id)
        if not path.exists():
            raise ArtifactError("binding not found")
        previous = self.binding(binding.base_id, binding.tenant_id)
        if binding.version != previous.version + 1:
            raise ArtifactError("binding version must increase by one")
        content = self._stored_binding(binding)
        previous_path = self.binding_version_path(previous.base_id, previous.tenant_id, previous.version)
        if not previous_path.exists():
            with previous_path.open("x") as stream:
                stream.write(self._stored_binding(previous))
        version_path = self.binding_version_path(binding.base_id, binding.tenant_id, binding.version)
        with version_path.open("x") as stream:
            stream.write(content)
        with NamedTemporaryFile(mode="w", dir=self.root, prefix=".binding-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
