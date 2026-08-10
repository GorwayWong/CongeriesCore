"""AgentSpec composition and the minimal direct Agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field

from congeries_core.policy.authorization import ResourceRef
from congeries_core.provider.context import (
    ContextBinding,
    ContextCompleteness,
    ContextCompletenessPolicy,
    ContextEntry,
    ContextResolver,
)
from congeries_core.provider.model import (
    ModelBinding,
    ModelGateway,
    ModelRequest,
    ModelResponse,
)
from congeries_core.runtime.content import ContentBlock
from congeries_core.runtime.context import RuntimeCallContext
from congeries_core.runtime.control import Clock
from congeries_core.runtime.errors import (
    CoreError,
    ErrorCategory,
    ErrorDetail,
    core_error,
)
from congeries_core.runtime.ids import AgentId, DefinitionId, RunId
from congeries_core.runtime.json_types import as_array, as_object
from congeries_core.runtime.run import AgentRun, Run, RunStatus
from congeries_core.state.service import RunService


@dataclass(frozen=True, slots=True)
class AgentSpec:
    agent_id: AgentId
    definition_id: DefinitionId
    instructions: tuple[ContentBlock, ...]
    context_binding: ContextBinding
    model_binding: ModelBinding
    skill_refs: tuple[ResourceRef, ...] = field(default_factory=tuple)
    tool_refs: tuple[ResourceRef, ...] = field(default_factory=tuple)
    policy_ref: ResourceRef | None = None

    def __post_init__(self) -> None:
        if not self.instructions:
            raise ValueError("AgentSpec instructions must not be empty")
        if len({item.key for item in self.skill_refs}) != len(self.skill_refs):
            raise ValueError("AgentSpec skill references must be unique")
        if len({item.key for item in self.tool_refs}) != len(self.tool_refs):
            raise ValueError("AgentSpec tool references must be unique")

    def to_data(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id.value,
            "definition_id": self.definition_id.value,
            "instructions": [item.to_data() for item in self.instructions],
            "skill_refs": [item.to_data() for item in self.skill_refs],
            "tool_refs": [item.to_data() for item in self.tool_refs],
            "context_binding": self.context_binding.to_data(),
            "policy_ref": self.policy_ref.to_data() if self.policy_ref else None,
            "model_binding": self.model_binding.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> AgentSpec:
        raw_policy = data.get("policy_ref")
        return cls(
            agent_id=AgentId(str(data["agent_id"])),
            definition_id=DefinitionId(str(data["definition_id"])),
            instructions=tuple(
                ContentBlock.from_data(as_object(item, "Agent instruction"))
                for item in as_array(data["instructions"], "Agent instructions")
            ),
            skill_refs=tuple(
                ResourceRef.from_data(as_object(item, "Skill reference"))
                for item in as_array(data["skill_refs"], "Skill references")
            ),
            tool_refs=tuple(
                ResourceRef.from_data(as_object(item, "Tool reference"))
                for item in as_array(data["tool_refs"], "Tool references")
            ),
            context_binding=ContextBinding.from_data(
                as_object(data["context_binding"], "Context binding")
            ),
            policy_ref=(
                ResourceRef.from_data(as_object(raw_policy, "Policy reference"))
                if raw_policy is not None
                else None
            ),
            model_binding=ModelBinding.from_data(
                as_object(data["model_binding"], "Model binding")
            ),
        )


class AgentRegistry:
    def __init__(self) -> None:
        self._specs: dict[tuple[AgentId, DefinitionId], AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        key = spec.agent_id, spec.definition_id
        if key in self._specs:
            raise core_error(
                ErrorCategory.CONFLICT,
                "agent_spec_already_registered",
                "AgentSpec is already registered",
            )
        self._specs[key] = spec

    def get(self, agent_id: AgentId, definition_id: DefinitionId) -> AgentSpec:
        spec = self._specs.get((agent_id, definition_id))
        if spec is None:
            raise core_error(
                ErrorCategory.UNAVAILABLE,
                "agent_spec_not_registered",
                "AgentSpec is not registered",
            )
        return spec


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    run: AgentRun
    response: ModelResponse | None = None
    error: ErrorDetail | None = None

    def __post_init__(self) -> None:
        if self.run.status is RunStatus.SUCCEEDED:
            if self.response is None or self.error is not None:
                raise ValueError("successful Agent execution requires only a response")
        elif self.run.status in {
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.PAUSED,
        }:
            if self.response is not None or self.error is None:
                raise ValueError("unsuccessful Agent execution requires only an error")
        else:
            raise ValueError("Agent execution result requires a final or paused Run")

    def to_data(self) -> dict[str, object]:
        return {
            "run": self.run.to_data(),
            "response": self.response.to_data() if self.response else None,
            "error": self.error.to_data() if self.error else None,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> AgentExecutionResult:
        decoded_run = Run.from_data(as_object(data["run"], "Agent Run"))
        if not isinstance(decoded_run, AgentRun):
            raise ValueError("Agent execution result requires an AgentRun")
        raw_response = data.get("response")
        raw_error = data.get("error")
        return cls(
            run=decoded_run,
            response=(
                ModelResponse.from_data(as_object(raw_response, "Model response"))
                if raw_response is not None
                else None
            ),
            error=(
                ErrorDetail.from_data(as_object(raw_error, "Agent error"))
                if raw_error is not None
                else None
            ),
        )


class AgentRuntime:
    def __init__(
        self,
        *,
        agents: AgentRegistry,
        contexts: ContextResolver,
        models: ModelGateway,
        runs: RunService,
        clock: Clock,
    ) -> None:
        self._agents = agents
        self._contexts = contexts
        self._models = models
        self._runs = runs
        self._clock = clock

    async def execute(
        self,
        run_id: RunId,
        input: tuple[ContentBlock, ...],
        context: RuntimeCallContext,
    ) -> AgentExecutionResult:
        if not input:
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "agent_input_required",
                "Agent execution input must not be empty",
            )
        loaded = await self._runs.get(run_id)
        if not isinstance(loaded, AgentRun):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "agent_run_required",
                "AgentRuntime requires an AgentRun",
            )
        self._validate_context(loaded, context)
        if loaded.status is not RunStatus.CREATED:
            raise core_error(
                ErrorCategory.CONFLICT,
                "agent_run_already_started",
                "minimal Agent execution requires a CREATED AgentRun",
            )

        current = loaded
        try:
            context.check_active(self._clock)
            spec = self._agents.get(loaded.agent_id, loaded.definition_id)
            if spec.model_binding.ref != loaded.model_binding_ref:
                raise core_error(
                    ErrorCategory.INVALID_REQUEST,
                    "agent_model_binding_mismatch",
                    "AgentRun model binding does not match AgentSpec",
                )
            current = await self._runs.start(run_id, current.state_version)
            current = await self._runs.advance(
                run_id, current.state_version, RunStatus.CONTEXT_LOADING
            )
            request = spec.context_binding.request(spec.definition_id, context)
            resolved = await self._contexts.resolve(request, spec.context_binding)
            if (
                resolved.completeness is ContextCompleteness.PARTIAL
                and spec.context_binding.completeness_policy
                is ContextCompletenessPolicy.REQUIRE_COMPLETE
            ):
                raise core_error(
                    ErrorCategory.PARTIAL_RESULT,
                    "partial_context_rejected",
                    "Agent context policy requires complete context",
                )
            current = await self._runs.advance(
                run_id, current.state_version, RunStatus.RUNNING
            )
            response = await self._generate(spec, input, resolved.entries, context)
            if response.tool_requests and not spec.tool_refs:
                raise core_error(
                    ErrorCategory.PROTOCOL_FAILURE,
                    "unexpected_tool_request",
                    "model proposed a Tool for an Agent without Tools",
                )
            context.check_active(self._clock)
            try:
                completed = await self._runs.complete(run_id, current.state_version)
            except CoreError as error:
                if error.detail.category is not ErrorCategory.CONFLICT:
                    raise
                latest = await self._runs.get(run_id)
                if (
                    isinstance(latest, AgentRun)
                    and latest.status is RunStatus.SUCCEEDED
                ):
                    completed = latest
                else:
                    return self._competing_result(latest, error.detail)
            if not isinstance(completed, AgentRun):
                raise AssertionError("AgentRun completion changed Run kind")
            return AgentExecutionResult(completed, response=response)
        except CoreError as error:
            terminal = await self._terminalize(run_id, error.detail)
            if terminal.status is RunStatus.SUCCEEDED:
                raise
            return AgentExecutionResult(
                terminal, error=self._run_error(terminal, error.detail)
            )

    async def _generate(
        self,
        spec: AgentSpec,
        input: tuple[ContentBlock, ...],
        context_entries: tuple[ContextEntry, ...],
        context: RuntimeCallContext,
    ) -> ModelResponse:
        last_error: CoreError | None = None
        for selector in spec.model_binding.selectors:
            try:
                capabilities = await self._models.capabilities(selector, context)
                if not capabilities.satisfies(spec.model_binding.required):
                    last_error = core_error(
                        ErrorCategory.UNSUPPORTED_CAPABILITY,
                        "model_binding_incompatible",
                        "model does not satisfy Agent binding requirements",
                    )
                    continue
                request = ModelRequest(
                    selector=selector,
                    input=input,
                    instructions=spec.instructions,
                    context_entries=context_entries,
                    tools=spec.tool_refs,
                    policy=spec.model_binding.default_policy,
                    budget=spec.model_binding.default_budget,
                )
                return await self._models.generate(request, context)
            except CoreError as error:
                if error.detail.category in {
                    ErrorCategory.UNAVAILABLE,
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                }:
                    last_error = error
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise core_error(
            ErrorCategory.UNAVAILABLE,
            "model_binding_unavailable",
            "no ModelProvider binding is available",
        )

    async def _terminalize(self, run_id: RunId, error: ErrorDetail) -> AgentRun:
        for _ in range(2):
            current = await self._runs.get(run_id)
            if not isinstance(current, AgentRun):
                raise AssertionError("AgentRun changed Run kind")
            if current.status.terminal or current.status is RunStatus.PAUSED:
                return current
            try:
                if error.category is ErrorCategory.CANCELLED:
                    result = await self._runs.cancel(run_id, current.state_version)
                else:
                    result = await self._runs.fail(run_id, current.state_version, error)
                if not isinstance(result, AgentRun):
                    raise AssertionError("AgentRun terminalization changed Run kind")
                return result
            except CoreError as conflict:
                if conflict.detail.category is not ErrorCategory.CONFLICT:
                    raise
        latest = await self._runs.get(run_id)
        if not isinstance(latest, AgentRun):
            raise AssertionError("AgentRun changed Run kind")
        if latest.status.terminal or latest.status is RunStatus.PAUSED:
            return latest
        raise core_error(
            ErrorCategory.CONFLICT,
            "agent_terminalization_conflict",
            "Agent Run state changed while terminalizing",
            retryable=True,
        )

    def _validate_context(self, run: AgentRun, context: RuntimeCallContext) -> None:
        if (
            context.run_id != run.run_id
            or context.root_run_id != run.root_run_id
            or context.parent_run_id != run.parent_run_id
            or context.workspace_id != run.workspace_id
            or context.session_ref != run.session_ref
            or context.scope != run.scope
        ):
            raise core_error(
                ErrorCategory.INVALID_REQUEST,
                "agent_call_context_mismatch",
                "RuntimeCallContext does not match AgentRun boundaries",
            )

    def _competing_result(
        self, run: object, fallback: ErrorDetail
    ) -> AgentExecutionResult:
        if not isinstance(run, AgentRun):
            raise AssertionError("AgentRun changed Run kind")
        if run.status not in {RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PAUSED}:
            raise CoreError(fallback)
        return AgentExecutionResult(run, error=self._run_error(run, fallback))

    def _run_error(self, run: AgentRun, fallback: ErrorDetail) -> ErrorDetail:
        if run.error_summary is not None:
            return run.error_summary.detail
        if run.status is RunStatus.CANCELLED:
            return ErrorDetail(
                ErrorCategory.CANCELLED,
                "run_cancelled",
                "Agent Run was cancelled",
            )
        return fallback
