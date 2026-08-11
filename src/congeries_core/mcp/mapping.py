"""Pure MCP binding and discovery validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Never

from congeries_core.runtime.errors import ErrorCategory, core_error
from congeries_core.runtime.json_types import JsonValue
from congeries_core.runtime.schema import SchemaRegistry
from congeries_core.tool.model import ToolDescriptor

from .model import (
    MCP_PROTOCOL_VERSION,
    McpAdapterDescriptor,
    McpContextBinding,
    McpDiscoverySnapshot,
    McpRemoteResource,
    McpRemoteTool,
    McpToolBinding,
)


def canonical_schema_digest(schema: JsonValue) -> str:
    try:
        encoded = json.dumps(
            schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("MCP Schema must be canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def validate_tool_binding(binding: McpToolBinding, descriptor: ToolDescriptor) -> None:
    if binding.tool != descriptor.ref:
        _invalid_binding("MCP Tool binding reference does not match its local Tool")
    if (
        binding.input_schema != descriptor.input_schema
        or binding.output_schema != descriptor.output_schema
    ):
        _invalid_binding("MCP Tool binding Schemas do not match its local Tool")


def validate_context_binding(
    binding: McpContextBinding, schemas: SchemaRegistry
) -> None:
    for requirement in binding.requirements:
        if not schemas.contains(requirement.schema):
            _invalid_binding("MCP Context binding Schema is not registered")


def validate_discovery(
    descriptor: McpAdapterDescriptor,
    snapshot: McpDiscoverySnapshot,
) -> McpDiscoverySnapshot:
    """Match a remote snapshot to the frozen local allowlist.

    This function is pure: it neither registers remote Schemas nor publishes
    capabilities. A caller may proceed only with the filtered snapshot returned
    here, never with the transport's untrusted snapshot directly.
    """

    if snapshot.protocol_version != MCP_PROTOCOL_VERSION:
        raise core_error(
            ErrorCategory.VERSION_MISMATCH,
            "mcp_protocol_version_mismatch",
            "remote MCP protocol version is unsupported",
        )
    if snapshot.protocol_version != descriptor.protocol_version:
        raise core_error(
            ErrorCategory.VERSION_MISMATCH,
            "mcp_protocol_version_mismatch",
            "remote MCP protocol version does not match the Adapter",
        )
    if snapshot.server.service_id != descriptor.service_id:
        raise core_error(
            ErrorCategory.PROTOCOL_FAILURE,
            "mcp_service_identity_mismatch",
            "remote MCP service identity does not match the Adapter",
        )

    # Iterate the local bindings, not the advertised records. This makes the
    # descriptor the source of authority and leaves unknown remote Tools unused.
    discovered_tools = {item.name: item for item in snapshot.tools}
    tools: list[McpRemoteTool] = []
    for binding in descriptor.tool_bindings:
        remote = discovered_tools.get(binding.remote_name)
        if remote is None:
            _missing("Tool", binding.remote_name)
        if canonical_schema_digest(remote.input_schema) != binding.input_schema_digest:
            _schema_mismatch("MCP Tool input Schema changed")
        if (
            canonical_schema_digest(remote.output_schema)
            != binding.output_schema_digest
        ):
            _schema_mismatch("MCP Tool output Schema changed")
        tools.append(remote)

    # Resource URIs follow the same allowlist rule. V1 deliberately has no URI
    # templates or enumeration path that could widen this exact match.
    discovered_resources = {item.uri: item for item in snapshot.resources}
    resources: list[McpRemoteResource] = []
    for binding in descriptor.context_bindings:
        remote = discovered_resources.get(binding.resource_uri)
        if remote is None:
            _missing("resource", binding.resource_uri)
        if canonical_schema_digest(remote.schema) != binding.resource_schema_digest:
            _schema_mismatch("MCP resource Schema changed")
        resources.append(remote)

    # Returning a copy with only bound records prevents later code from
    # accidentally treating a well-formed but undeclared advertisement as usable.
    return replace(snapshot, tools=tuple(tools), resources=tuple(resources))


def _invalid_binding(message: str) -> Never:
    raise core_error(
        ErrorCategory.INVALID_REQUEST,
        "invalid_mcp_binding",
        message,
    )


def _missing(kind: str, identity: str) -> Never:
    del identity
    raise core_error(
        ErrorCategory.UNSUPPORTED_CAPABILITY,
        "mcp_bound_capability_missing",
        f"bound MCP {kind} is not advertised",
    )


def _schema_mismatch(message: str) -> Never:
    raise core_error(
        ErrorCategory.VERSION_MISMATCH,
        "mcp_schema_mismatch",
        message,
    )
