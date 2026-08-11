"""Pure Plugin Manifest validation and environment preflight."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from congeries_core.runtime.errors import CoreError, ErrorCategory

from .errors import plugin_error
from .model import PluginManifest, PluginPermission, SemVer


class ManifestValidator:
    """Strict pure validator with no loader, registry, policy, or event effects."""

    def validate(self, data: Mapping[str, object]) -> PluginManifest:
        try:
            return PluginManifest.from_data(dict(data))
        except CoreError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise plugin_error(
                ErrorCategory.INVALID_REQUEST,
                "invalid_manifest",
                "Plugin manifest is invalid",
                cause_id=type(error).__name__,
            ) from error


class PermissionRepresentationChecker(Protocol):
    def can_represent(self, permission: PluginPermission) -> bool: ...


class AllowRepresentablePermissions:
    def can_represent(self, permission: PluginPermission) -> bool:
        del permission
        return True


class PluginPreflight:
    def __init__(
        self,
        *,
        core_api_version: SemVer,
        permissions: PermissionRepresentationChecker,
    ) -> None:
        self._core_api_version = core_api_version
        self._permissions = permissions

    def check_local(self, manifest: PluginManifest) -> None:
        if not manifest.core_api.matches(self._core_api_version):
            raise plugin_error(
                ErrorCategory.VERSION_MISMATCH,
                "incompatible_core_api",
                "Plugin is incompatible with the active Core API",
                plugin=manifest.name,
            )
        permissions = list(manifest.permissions)
        for declaration in manifest.provides:
            permissions.extend(declaration.permissions)
        if not all(self._permissions.can_represent(item) for item in permissions):
            raise plugin_error(
                ErrorCategory.DENIED,
                "permission_denied",
                "Plugin permission cannot be represented by active policy",
                plugin=manifest.name,
            )
