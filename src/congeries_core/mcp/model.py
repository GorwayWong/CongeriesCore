"""Immutable transport-neutral MCP Adapter v1 contracts."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from congeries_core.provider.context import ContextRequirement
from congeries_core.runtime.capability import CapabilityRef
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.ids import ProviderId
from congeries_core.runtime.json_types import (
    JsonValue,
    as_array,
    as_int,
    as_json_value,
    as_object,
)
from congeries_core.runtime.schema import SchemaRef

MCP_ADAPTER_CONTRACT_VERSION = "1"
MCP_PROTOCOL_VERSION = "2026-07-28"

if TYPE_CHECKING:
    from congeries_core.plugin.loader import LoadedCapability

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")


@dataclass(frozen=True, slots=True)
class McpToolBinding:
    tool: CapabilityRef
    remote_name: str
    input_schema: SchemaRef
    output_schema: SchemaRef
    input_schema_digest: str
    output_schema_digest: str

    def __post_init__(self) -> None:
        _require_capability(self.tool, "tool", "MCP Tool binding")
        _require_text(self.remote_name, "MCP remote Tool name")
        _require_digest(self.input_schema_digest, "MCP input Schema digest")
        _require_digest(self.output_schema_digest, "MCP output Schema digest")

    def to_data(self) -> dict[str, object]:
        return {
            "tool": self.tool.to_data(),
            "remote_name": self.remote_name,
            "input_schema": self.input_schema.to_data(),
            "output_schema": self.output_schema.to_data(),
            "input_schema_digest": self.input_schema_digest,
            "output_schema_digest": self.output_schema_digest,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpToolBinding:
        _require_fields(
            data,
            {
                "tool",
                "remote_name",
                "input_schema",
                "output_schema",
                "input_schema_digest",
                "output_schema_digest",
            },
            "MCP Tool binding",
        )
        return cls(
            tool=CapabilityRef.from_data(as_object(data["tool"], "MCP Tool")),
            remote_name=_string(data["remote_name"], "MCP remote Tool name"),
            input_schema=SchemaRef.from_data(
                as_object(data["input_schema"], "MCP input Schema")
            ),
            output_schema=SchemaRef.from_data(
                as_object(data["output_schema"], "MCP output Schema")
            ),
            input_schema_digest=_string(
                data["input_schema_digest"], "MCP input Schema digest"
            ),
            output_schema_digest=_string(
                data["output_schema_digest"], "MCP output Schema digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class McpContextBinding:
    provider: CapabilityRef
    resource_uri: str
    requirements: tuple[ContextRequirement, ...]
    resource_schema_digest: str

    def __post_init__(self) -> None:
        _require_capability(self.provider, "context_provider", "MCP Context binding")
        _require_exact_uri(self.resource_uri)
        if not self.requirements:
            raise ValueError("MCP Context binding requires Context requirements")
        keys = tuple(item.key for item in self.requirements)
        if len(set(keys)) != len(keys):
            raise ValueError("MCP Context requirements must be unique")
        _require_digest(self.resource_schema_digest, "MCP resource Schema digest")

    @property
    def provider_id(self) -> ProviderId:
        return ProviderId(self.provider.id.value)

    def to_data(self) -> dict[str, object]:
        return {
            "provider": self.provider.to_data(),
            "resource_uri": self.resource_uri,
            "requirements": [item.to_data() for item in self.requirements],
            "resource_schema_digest": self.resource_schema_digest,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpContextBinding:
        _require_fields(
            data,
            {
                "provider",
                "resource_uri",
                "requirements",
                "resource_schema_digest",
            },
            "MCP Context binding",
        )
        return cls(
            provider=CapabilityRef.from_data(
                as_object(data["provider"], "MCP Context provider")
            ),
            resource_uri=_string(data["resource_uri"], "MCP resource URI"),
            requirements=tuple(
                ContextRequirement.from_data(as_object(item, "MCP Context requirement"))
                for item in as_array(data["requirements"], "MCP Context requirements")
            ),
            resource_schema_digest=_string(
                data["resource_schema_digest"], "MCP resource Schema digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class McpAdapterDescriptor:
    ref: CapabilityRef
    service_id: str
    protocol_version: str = MCP_PROTOCOL_VERSION
    tool_bindings: tuple[McpToolBinding, ...] = field(default_factory=tuple)
    context_bindings: tuple[McpContextBinding, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_capability(self.ref, "mcp_adapter", "MCP Adapter descriptor")
        _require_text(self.service_id, "MCP service identity")
        if self.protocol_version != MCP_PROTOCOL_VERSION:
            raise ValueError("MCP protocol version is unsupported")
        if not self.tool_bindings and not self.context_bindings:
            raise ValueError("MCP Adapter requires at least one explicit binding")
        _require_unique(
            (item.tool.key for item in self.tool_bindings),
            "MCP local Tool bindings",
        )
        _require_unique(
            (item.remote_name for item in self.tool_bindings),
            "MCP remote Tool bindings",
        )
        _require_unique(
            (item.provider.key for item in self.context_bindings),
            "MCP Context provider bindings",
        )
        _require_unique(
            (item.resource_uri for item in self.context_bindings),
            "MCP resource bindings",
        )
        context_keys = (
            requirement.key
            for binding in self.context_bindings
            for requirement in binding.requirements
        )
        _require_unique(context_keys, "MCP Context key bindings")
        for capability in (
            *(item.tool for item in self.tool_bindings),
            *(item.provider for item in self.context_bindings),
        ):
            if capability.owning_extension != self.ref.owning_extension:
                raise ValueError("MCP bindings must share the Adapter owner")

    def tool_binding(self, ref: CapabilityRef) -> McpToolBinding:
        return _find_binding(
            self.tool_bindings,
            lambda item: item.tool == ref,
            "MCP Tool binding is not declared",
        )

    def context_binding(self, provider_id: ProviderId) -> McpContextBinding:
        return _find_binding(
            self.context_bindings,
            lambda item: item.provider_id == provider_id,
            "MCP Context binding is not declared",
        )

    def to_data(self) -> dict[str, object]:
        return {
            "ref": self.ref.to_data(),
            "service_id": self.service_id,
            "protocol_version": self.protocol_version,
            "tool_bindings": [item.to_data() for item in self.tool_bindings],
            "context_bindings": [item.to_data() for item in self.context_bindings],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpAdapterDescriptor:
        _require_fields(
            data,
            {
                "ref",
                "service_id",
                "protocol_version",
                "tool_bindings",
                "context_bindings",
            },
            "MCP Adapter descriptor",
        )
        return cls(
            ref=CapabilityRef.from_data(as_object(data["ref"], "MCP Adapter")),
            service_id=_string(data["service_id"], "MCP service identity"),
            protocol_version=_string(data["protocol_version"], "MCP protocol version"),
            tool_bindings=tuple(
                McpToolBinding.from_data(as_object(item, "MCP Tool binding"))
                for item in as_array(data["tool_bindings"], "MCP Tool bindings")
            ),
            context_bindings=tuple(
                McpContextBinding.from_data(as_object(item, "MCP Context binding"))
                for item in as_array(data["context_bindings"], "MCP Context bindings")
            ),
        )


@dataclass(frozen=True, slots=True)
class McpServerInfo:
    service_id: str
    name: str
    version: str

    def __post_init__(self) -> None:
        _require_text(self.service_id, "MCP server service identity")
        _require_text(self.name, "MCP server name")
        _require_text(self.version, "MCP server version")

    def to_data(self) -> dict[str, str]:
        return {
            "service_id": self.service_id,
            "name": self.name,
            "version": self.version,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpServerInfo:
        _require_fields(data, {"service_id", "name", "version"}, "MCP server info")
        return cls(
            _string(data["service_id"], "MCP server service identity"),
            _string(data["name"], "MCP server name"),
            _string(data["version"], "MCP server version"),
        )


@dataclass(frozen=True, slots=True)
class McpRemoteTool:
    name: str
    input_schema: JsonValue
    output_schema: JsonValue

    def __post_init__(self) -> None:
        _require_text(self.name, "MCP discovered Tool name")
        object.__setattr__(
            self, "input_schema", as_json_value(self.input_schema, "MCP input Schema")
        )
        object.__setattr__(
            self,
            "output_schema",
            as_json_value(self.output_schema, "MCP output Schema"),
        )

    def to_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpRemoteTool:
        _require_fields(data, {"name", "input_schema", "output_schema"}, "MCP Tool")
        return cls(
            _string(data["name"], "MCP discovered Tool name"),
            as_json_value(data.get("input_schema"), "MCP input Schema"),
            as_json_value(data.get("output_schema"), "MCP output Schema"),
        )


@dataclass(frozen=True, slots=True)
class McpRemoteResource:
    uri: str
    media_type: str
    schema: JsonValue

    def __post_init__(self) -> None:
        _require_exact_uri(self.uri)
        _require_text(self.media_type, "MCP resource media type")
        object.__setattr__(
            self, "schema", as_json_value(self.schema, "MCP resource Schema")
        )

    def to_data(self) -> dict[str, object]:
        return {"uri": self.uri, "media_type": self.media_type, "schema": self.schema}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpRemoteResource:
        _require_fields(data, {"uri", "media_type", "schema"}, "MCP resource")
        return cls(
            _string(data["uri"], "MCP resource URI"),
            _string(data["media_type"], "MCP resource media type"),
            as_json_value(data.get("schema"), "MCP resource Schema"),
        )


@dataclass(frozen=True, slots=True)
class McpDiscoverySnapshot:
    protocol_version: str
    server: McpServerInfo
    discovery_identity: str
    tools: tuple[McpRemoteTool, ...] = field(default_factory=tuple)
    resources: tuple[McpRemoteResource, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_text(self.protocol_version, "MCP discovery protocol version")
        _require_text(self.discovery_identity, "MCP discovery identity")
        _require_unique((item.name for item in self.tools), "MCP discovered Tools")
        _require_unique(
            (item.uri for item in self.resources), "MCP discovered resources"
        )

    def to_data(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "server": self.server.to_data(),
            "discovery_identity": self.discovery_identity,
            "tools": [item.to_data() for item in self.tools],
            "resources": [item.to_data() for item in self.resources],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpDiscoverySnapshot:
        _require_fields(
            data,
            {"protocol_version", "server", "discovery_identity", "tools", "resources"},
            "MCP discovery snapshot",
        )
        return cls(
            protocol_version=_string(
                data["protocol_version"], "MCP discovery protocol version"
            ),
            server=McpServerInfo.from_data(
                as_object(data["server"], "MCP server info")
            ),
            discovery_identity=_string(
                data["discovery_identity"], "MCP discovery identity"
            ),
            tools=tuple(
                McpRemoteTool.from_data(as_object(item, "MCP discovered Tool"))
                for item in as_array(data["tools"], "MCP discovered Tools")
            ),
            resources=tuple(
                McpRemoteResource.from_data(as_object(item, "MCP discovered resource"))
                for item in as_array(data["resources"], "MCP discovered resources")
            ),
        )


@dataclass(frozen=True, slots=True)
class McpToolRequest:
    service_id: str
    name: str
    arguments: JsonValue
    operation_identity: str
    attempt: int

    def __post_init__(self) -> None:
        _require_text(self.service_id, "MCP Tool request service identity")
        _require_text(self.name, "MCP Tool request name")
        _require_text(self.operation_identity, "MCP Tool operation identity")
        _require_positive_int(self.attempt, "MCP Tool attempt")
        object.__setattr__(
            self, "arguments", as_json_value(self.arguments, "MCP Tool arguments")
        )

    def to_data(self) -> dict[str, object]:
        return {
            "service_id": self.service_id,
            "name": self.name,
            "arguments": self.arguments,
            "operation_identity": self.operation_identity,
            "attempt": self.attempt,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpToolRequest:
        _require_fields(
            data,
            {"service_id", "name", "arguments", "operation_identity", "attempt"},
            "MCP Tool request",
        )
        return cls(
            service_id=_string(data["service_id"], "MCP Tool service identity"),
            name=_string(data["name"], "MCP Tool request name"),
            arguments=as_json_value(data.get("arguments"), "MCP Tool arguments"),
            operation_identity=_string(
                data["operation_identity"], "MCP Tool operation identity"
            ),
            attempt=as_int(data["attempt"], "MCP Tool attempt"),
        )


@dataclass(frozen=True, slots=True)
class McpToolResponse:
    output: JsonValue

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "output", as_json_value(self.output, "MCP Tool output")
        )

    def to_data(self) -> dict[str, JsonValue]:
        return {"output": self.output}

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpToolResponse:
        _require_fields(data, {"output"}, "MCP Tool response")
        return cls(as_json_value(data.get("output"), "MCP Tool output"))


@dataclass(frozen=True, slots=True)
class McpResourceRequest:
    service_id: str
    uri: str
    keys: tuple[str, ...]
    operation_identity: str

    def __post_init__(self) -> None:
        _require_text(self.service_id, "MCP resource request service identity")
        _require_exact_uri(self.uri)
        if not self.keys or len(set(self.keys)) != len(self.keys):
            raise ValueError("MCP resource request keys must be non-empty and unique")
        for key in self.keys:
            _require_text(key, "MCP resource request key")
        _require_text(self.operation_identity, "MCP resource operation identity")

    def to_data(self) -> dict[str, object]:
        return {
            "service_id": self.service_id,
            "uri": self.uri,
            "keys": list(self.keys),
            "operation_identity": self.operation_identity,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpResourceRequest:
        _require_fields(
            data,
            {"service_id", "uri", "keys", "operation_identity"},
            "MCP resource request",
        )
        return cls(
            service_id=_string(data["service_id"], "MCP resource service identity"),
            uri=_string(data["uri"], "MCP resource URI"),
            keys=tuple(
                _string(item, "MCP resource request key")
                for item in as_array(data["keys"], "MCP resource request keys")
            ),
            operation_identity=_string(
                data["operation_identity"], "MCP resource operation identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class McpResourceResponse:
    uri: str
    media_type: str
    contents: JsonValue

    def __post_init__(self) -> None:
        _require_exact_uri(self.uri)
        _require_text(self.media_type, "MCP resource response media type")
        object.__setattr__(
            self, "contents", as_json_value(self.contents, "MCP resource contents")
        )

    def to_data(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "media_type": self.media_type,
            "contents": self.contents,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> McpResourceResponse:
        _require_fields(
            data, {"uri", "media_type", "contents"}, "MCP resource response"
        )
        return cls(
            _string(data["uri"], "MCP resource response URI"),
            _string(data["media_type"], "MCP resource response media type"),
            as_json_value(data.get("contents"), "MCP resource contents"),
        )


class McpTransport(Protocol):
    @property
    def kind(self) -> str: ...

    async def discover(self, context: RuntimeCallContext) -> McpDiscoverySnapshot: ...

    async def call_tool(
        self, request: McpToolRequest, context: RuntimeCallContext
    ) -> McpToolResponse: ...

    async def read_resource(
        self, request: McpResourceRequest, context: RuntimeCallContext
    ) -> McpResourceResponse: ...


@dataclass(frozen=True, slots=True)
class McpAdapterImplementation:
    descriptor: McpAdapterDescriptor
    transport: McpTransport

    def __post_init__(self) -> None:
        if not _SAFE_KIND_PATTERN.fullmatch(self.transport.kind):
            raise ValueError("MCP transport kind is invalid")

    def validate_composition(
        self, plugin_id: str, capabilities: tuple[LoadedCapability, ...]
    ) -> None:
        from .composition import validate_mcp_composition

        validate_mcp_composition(self, plugin_id, capabilities)


def _require_capability(ref: CapabilityRef, kind: str, name: str) -> None:
    if (
        ref.namespace != "core"
        or ref.kind != kind
        or ref.contract_version != MCP_ADAPTER_CONTRACT_VERSION
    ):
        raise ValueError(f"{name} requires a core {kind} v1 reference")


def _require_text(value: str, name: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and trimmed")


def _require_digest(value: str, name: str) -> None:
    if not _DIGEST_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be lowercase SHA-256")


def _require_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _require_exact_uri(value: str) -> None:
    _require_text(value, "MCP resource URI")
    if any(character in value for character in ("*", "{", "}")):
        raise ValueError("MCP resource URI templates and wildcards are unsupported")
    if not urlsplit(value).scheme:
        raise ValueError("MCP resource URI must be absolute")


def _require_fields(data: dict[str, object], expected: set[str], name: str) -> None:
    if set(data) != expected:
        raise ValueError(f"{name} fields are invalid")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _require_unique[T](values: Iterable[T], name: str) -> None:
    items = tuple(values)
    if len(set(items)) != len(items):
        raise ValueError(f"{name} must be unique")


def _find_binding[T](
    bindings: tuple[T, ...], predicate: Callable[[T], bool], message: str
) -> T:
    for binding in bindings:
        if predicate(binding):
            return binding
    raise ValueError(message)
