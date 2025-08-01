# Copyright 2025 Emcie Co Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from itertools import chain
from pprint import pformat
import traceback
from typing import Optional, Sequence, cast
from croniter import croniter
from typing_extensions import override

from parlant.core import async_utils
from parlant.core.agents import Agent, AgentId, CompositionMode
from parlant.core.capabilities import Capability
from parlant.core.common import CancellationSuppressionLatch
from parlant.core.context_variables import (
    ContextVariable,
    ContextVariableValue,
    ContextVariableStore,
)
from parlant.core.emission.event_buffer import EventBuffer
from parlant.core.engines.alpha.loaded_context import Interaction, LoadedContext, ResponseState
from parlant.core.engines.alpha.message_generator import MessageGenerator
from parlant.core.engines.alpha.hooks import EngineHooks
from parlant.core.engines.alpha.perceived_performance_policy import PerceivedPerformancePolicy
from parlant.core.engines.alpha.relational_guideline_resolver import RelationalGuidelineResolver
from parlant.core.engines.alpha.tool_calling.tool_caller import (
    MissingToolData,
    ToolInsights,
    InvalidToolData,
    ProblematicToolData,
)
from parlant.core.engines.alpha.utterance_selector import UtteranceSelector
from parlant.core.engines.alpha.message_event_composer import (
    MessageEventComposer,
)
from parlant.core.guidelines import Guideline, GuidelineId, GuidelineContent
from parlant.core.glossary import Term
from parlant.core.journeys import Journey
from parlant.core.sessions import (
    AgentState,
    ContextVariable as StoredContextVariable,
    EventKind,
    GuidelineMatch as StoredGuidelineMatch,
    GuidelineMatchingInspection,
    MessageGenerationInspection,
    PreparationIteration,
    PreparationIterationGenerations,
    Session,
    SessionUpdateParams,
    Term as StoredTerm,
    ToolEventData,
)
from parlant.core.engines.alpha.guideline_matching.guideline_matcher import (
    GuidelineMatcher,
    GuidelineMatchingResult,
)
from parlant.core.engines.alpha.guideline_matching.guideline_match import GuidelineMatch
from parlant.core.engines.alpha.tool_event_generator import (
    ToolEventGenerationResult,
    ToolEventGenerator,
    ToolPreexecutionState,
)
from parlant.core.engines.alpha.utils import context_variables_to_json
from parlant.core.engines.types import Context, Engine, UtteranceReason, UtteranceRequest
from parlant.core.emissions import EventEmitter, EmittedEvent
from parlant.core.contextual_correlator import ContextualCorrelator
from parlant.core.loggers import Logger
from parlant.core.entity_cq import EntityQueries, EntityCommands
from parlant.core.tools import ToolContext, ToolId


class _PreparationIterationResolution(Enum):
    COMPLETED = "continue"
    """Continue with the next preparation iteration"""

    BAIL = "bail"
    """Bail out of the preparation iterations, as requested by a hook"""


@dataclass
class _PreparationIterationResult:
    resolution: _PreparationIterationResolution
    inspection: PreparationIteration | None = field(default=None)


class AlphaEngine(Engine):
    """The main AI processing engine (as of Feb 25, the latest and greatest processing engine)"""

    def __init__(
        self,
        logger: Logger,
        correlator: ContextualCorrelator,
        entity_queries: EntityQueries,
        entity_commands: EntityCommands,
        guideline_matcher: GuidelineMatcher,
        relational_guideline_resolver: RelationalGuidelineResolver,
        tool_event_generator: ToolEventGenerator,
        fluid_message_generator: MessageGenerator,
        utterance_selector: UtteranceSelector,
        perceived_performance_policy: PerceivedPerformancePolicy,
        hooks: EngineHooks,
    ) -> None:
        self._logger = logger
        self._correlator = correlator

        self._entity_queries = entity_queries
        self._entity_commands = entity_commands

        self._guideline_matcher = guideline_matcher
        self._relational_guideline_resolver = relational_guideline_resolver
        self._tool_event_generator = tool_event_generator
        self._fluid_message_generator = fluid_message_generator
        self._utterance_selector = utterance_selector
        self._perceived_performance_policy = perceived_performance_policy

        self._hooks = hooks

    @override
    async def process(
        self,
        context: Context,
        event_emitter: EventEmitter,
    ) -> bool:
        """Processes a context and emits new events as needed"""

        # Load the full relevant information from storage.
        loaded_context = await self._load_context(context, event_emitter)

        if loaded_context.session.mode == "manual":
            return True

        try:
            with self._logger.operation(f"Processing context for session {context.session_id}"):
                await self._do_process(loaded_context)
            return True
        except asyncio.CancelledError:
            return False
        except Exception as exc:
            formatted_exception = pformat(traceback.format_exception(exc))

            self._logger.error(f"Processing error: {formatted_exception}")

            if await self._hooks.call_on_error(loaded_context, exc):
                await self._emit_error_event(loaded_context, formatted_exception)

            return False
        except BaseException as exc:
            self._logger.critical(f"Critical processing error: {traceback.format_exception(exc)}")
            raise

    @override
    async def utter(
        self,
        context: Context,
        event_emitter: EventEmitter,
        requests: Sequence[UtteranceRequest],
    ) -> bool:
        """Produces a new message into a session, guided by specific utterance requests"""

        # Load the full relevant information from storage.
        loaded_context = await self._load_context(
            context,
            event_emitter,
            # Results seem to be more consistent with the requests
            # if we ignore the interaction's content.
            load_interaction=False,
        )

        try:
            with self._logger.operation(f"Uttering in session {context.session_id}"):
                await self._do_utter(loaded_context, requests)
            return True
        except asyncio.CancelledError:
            self._logger.warning(f"Uttering in session {context.session_id} was cancelled.")
            return False
        except Exception as exc:
            formatted_exception = pformat(traceback.format_exception(exc))

            self._logger.error(
                f"Error during uttering in session {context.session_id}: {formatted_exception}"
            )

            if await self._hooks.call_on_error(loaded_context, exc):
                await self._emit_error_event(loaded_context, formatted_exception)

            return False
        except BaseException as exc:
            self._logger.critical(
                f"Critical error during uttering in session {context.session_id}: "
                f"{traceback.format_exception(type(exc), exc, exc.__traceback__)}"
            )
            raise

    async def _load_interaction_state(self, context: Context) -> Interaction:
        history = await self._entity_queries.find_events(context.session_id)
        last_known_event_offset = history[-1].offset if history else -1

        return Interaction(
            history=history,
            last_known_event_offset=last_known_event_offset,
        )

    async def _do_process(
        self,
        context: LoadedContext,
    ) -> None:
        if not await self._hooks.call_on_acknowledging(context):
            return  # Hook requested to bail out

        # Mark that this latest session state has been seen by the agent.
        await self._emit_acknowledgement_event(context)

        if not await self._hooks.call_on_acknowledged(context):
            return  # Hook requested to bail out

        try:
            if not await self._hooks.call_on_preparing(context):
                return  # Hook requested to bail out

            await self._initialize_response_state(context)
            preparation_iteration_inspections = []

            while not context.state.prepared_to_respond:
                # Need more data before we're ready to respond

                preamble_task = await self._get_preamble_task(context)

                if not await self._hooks.call_on_preparation_iteration_start(context):
                    break  # Hook requested to finish preparing

                # Get more data (guidelines, tools, etc.,)
                # This happens in iterations in order to support a feedback loop
                # where particular tool-call results may trigger new or different
                # guidelines that we need to follow.
                iteration_result = await self._run_preparation_iteration(context, preamble_task)

                if iteration_result.resolution == _PreparationIterationResolution.BAIL:
                    return
                else:
                    assert iteration_result.inspection

                # Save results for later inspection.
                preparation_iteration_inspections.append(iteration_result.inspection)

                # Some tools may update session mode (e.g. from automatic to manual).
                # This is particularly important to support human handoff.
                await self._update_session_mode(context)

                if not await self._hooks.call_on_preparation_iteration_end(context):
                    break

            if not await self._hooks.call_on_generating_messages(context):
                return

            # Filter missing and invalid tool parameters jointly
            problematic_data = await self._filter_problematic_tool_parameters(
                list(context.state.tool_insights.missing_data)
                + list(context.state.tool_insights.invalid_data)
            )
            context.state.tool_insights = ToolInsights(
                missing_data=[p for p in problematic_data if isinstance(p, MissingToolData)],
                invalid_data=[p for p in problematic_data if isinstance(p, InvalidToolData)],
            )
            with CancellationSuppressionLatch() as latch:
                # Money time: communicate with the customer given
                # all of the information we have prepared.
                message_generation_inspections = await self._generate_messages(context, latch)

                # Mark that the agent is ready to receive and respond to new events.
                await self._emit_ready_event(context)

                # Save results for later inspection.
                await self._entity_commands.create_inspection(
                    session_id=context.session.id,
                    correlation_id=self._correlator.correlation_id,
                    preparation_iterations=preparation_iteration_inspections,
                    message_generations=message_generation_inspections,
                )

                await self._add_agent_state(
                    context=context,
                    session=context.session,
                    guideline_matches=list(
                        chain(
                            context.state.ordinary_guideline_matches,
                            context.state.tool_enabled_guideline_matches,
                        )
                    ),
                )

                await self._hooks.call_on_generated_messages(context)

        except asyncio.CancelledError:
            # Task was cancelled. This usually happens for 1 of 2 reasons:
            #   1. The server is shutting down
            #   2. New information arrived and the currently loaded
            #      processing context is likely to be obsolete
            self._logger.warning("Processing cancelled")
            await self._emit_cancellation_event(context)
            await self._emit_ready_event(context)
            raise
        except Exception:
            # Mark that the agent is ready to receive and respond to new events.
            await self._emit_ready_event(context)
            raise

    async def _do_utter(
        self,
        context: LoadedContext,
        requests: Sequence[UtteranceRequest],
    ) -> None:
        try:
            await self._initialize_response_state(context)

            # Only use the specified utterance requests as guidelines here.
            context.state.ordinary_guideline_matches.extend(
                # Utterance requests are reduced to guidelines, to take advantage
                # of the engine's ability to consistently adhere to guidelines.
                await self._utterance_requests_to_guideline_matches(requests)
            )

            # Money time: communicate with the customer given the
            # specified utterance requests.
            with CancellationSuppressionLatch() as latch:
                message_generation_inspections = await self._generate_messages(context, latch)

            # Save results for later inspection.
            await self._entity_commands.create_inspection(
                session_id=context.session.id,
                correlation_id=self._correlator.correlation_id,
                preparation_iterations=[],
                message_generations=message_generation_inspections,
            )

        except asyncio.CancelledError:
            self._logger.warning("Uttering cancelled")
            raise
        finally:
            # Mark that the agent is ready to receive and respond to new events.
            await self._emit_ready_event(context)

    async def _load_context(
        self,
        context: Context,
        event_emitter: EventEmitter,
        load_interaction: bool = True,
    ) -> LoadedContext:
        # Load the full entities from storage.

        agent = await self._entity_queries.read_agent(context.agent_id)
        session = await self._entity_queries.read_session(context.session_id)
        customer = await self._entity_queries.read_customer(session.customer_id)

        if load_interaction:
            interaction = await self._load_interaction_state(context)
        else:
            interaction = Interaction([], -1)

        return LoadedContext(
            info=context,
            logger=self._logger,
            correlation_id=self._correlator.correlation_id,
            agent=agent,
            customer=customer,
            session=session,
            session_event_emitter=event_emitter,
            response_event_emitter=EventBuffer(agent),
            interaction=interaction,
            state=ResponseState(
                context_variables=[],
                glossary_terms=set(),
                capabilities=[],
                ordinary_guideline_matches=[],
                journeys=[],
                tool_enabled_guideline_matches={},
                tool_events=[],
                tool_insights=ToolInsights(),
                iterations_completed=0,
                prepared_to_respond=False,
                message_events=[],
            ),
        )

    async def _initialize_response_state(
        self,
        context: LoadedContext,
    ) -> None:
        # Load the relevant context variable values.
        context.state.context_variables = await self._load_context_variables(context)

        # Load relevant glossary terms and capabilities, initially based
        # mostly on the current interaction history.
        glossary, capabilities = await async_utils.safe_gather(
            self._load_glossary_terms(context),
            self._load_capabilities(context),
        )

        context.state.glossary_terms.update(glossary)
        context.state.capabilities = list(capabilities)

    async def _run_preparation_iteration(
        self,
        context: LoadedContext,
        preamble_task: asyncio.Task[bool],
    ) -> _PreparationIterationResult:
        # For optimization concerns, it's useful to capture the exact state
        # we were in before matching guidelines.
        tool_preexecution_state = await self._capture_tool_preexecution_state(context)

        # Match relevant guidelines, retrieving them in a
        # structured format such that we can distinguish
        # between ordinary and tool-enabled ones.
        (
            guideline_matching_result,
            context.state.ordinary_guideline_matches,
            context.state.tool_enabled_guideline_matches,
            context.state.journeys,
        ) = await self._load_matched_guidelines_and_journeys(context)

        if not await preamble_task:
            # Bail out on the rest of the processing, as the preamble
            # hook decided we should not proceed with processing.
            return _PreparationIterationResult(_PreparationIterationResolution.BAIL)

        # Matched guidelines may use glossary terms, so we need to ground our
        # response by reevaluating the relevant terms given these new guidelines.
        context.state.glossary_terms.update(await self._load_glossary_terms(context))

        # Infer any needed tool calls and execute them,
        # adding the resulting tool events to the session.
        if tool_calling_result := await self._call_tools(context, tool_preexecution_state):
            (
                tool_event_generation_result,
                new_tool_events,
                tool_insights,
            ) = tool_calling_result

            context.state.tool_events += new_tool_events
            context.state.tool_insights = tool_insights
        else:
            tool_event_generation_result = None
            new_tool_events = []

        # Tool calls may have returned with data that uses glossary terms,
        # so we need to ground our response again by reevaluating terms.
        context.state.glossary_terms.update(await self._load_glossary_terms(context))

        # Mark that another iteration has been completed
        # (this is important to avoid running more than K max iterations)
        context.state.iterations_completed += 1

        # If there's no new information to consider (which would have come from
        # the tools), then we can consider ourselves prepared to respond.
        if not new_tool_events:
            context.state.prepared_to_respond = True
        # Alternatively, we we've reached the max number of iterations,
        # we should just go ahead and respond anyway, despite possibly
        # needing more data for a fully accurate response.
        #
        # This is a trade-off that can be controlled by adjusting the max.
        elif context.state.iterations_completed == context.agent.max_engine_iterations:
            self._logger.warning(
                f"Reached max tool call iterations ({context.agent.max_engine_iterations})"
            )
            context.state.prepared_to_respond = True

        # Return structured inspection information, useful for later troubleshooting.
        return _PreparationIterationResult(
            _PreparationIterationResolution.COMPLETED,
            PreparationIteration(
                guideline_matches=[
                    StoredGuidelineMatch(
                        guideline_id=match.guideline.id,
                        condition=match.guideline.content.condition,
                        action=match.guideline.content.action or None,
                        score=match.score,
                        rationale=match.rationale,
                    )
                    for match in chain(
                        context.state.ordinary_guideline_matches,
                        context.state.tool_enabled_guideline_matches.keys(),
                    )
                ],
                tool_calls=[
                    tool_call
                    for tool_event in new_tool_events
                    for tool_call in cast(ToolEventData, tool_event.data)["tool_calls"]
                ],
                terms=[
                    StoredTerm(
                        id=term.id,
                        name=term.name,
                        description=term.description,
                        synonyms=list(term.synonyms),
                    )
                    for term in context.state.glossary_terms
                ],
                context_variables=[
                    StoredContextVariable(
                        id=variable.id,
                        name=variable.name,
                        description=variable.description,
                        key=context.session.customer_id,
                        value=value.data,
                    )
                    for variable, value in context.state.context_variables
                ],
                generations=PreparationIterationGenerations(
                    guideline_matching=GuidelineMatchingInspection(
                        total_duration=guideline_matching_result.total_duration,
                        batches=guideline_matching_result.batch_generations,
                    ),
                    tool_calls=tool_event_generation_result.generations
                    if tool_event_generation_result
                    else [],
                ),
            ),
        )

    async def _update_session_mode(self, context: LoadedContext) -> None:
        # Do we even have control-requests coming from any called tools?
        if tool_call_control_outputs := [
            tool_call["result"]["control"]
            for tool_event in context.state.tool_events
            for tool_call in cast(ToolEventData, tool_event.data)["tool_calls"]
        ]:
            # Yes we do. Update session mode as needed.

            current_session_mode = context.session.mode
            new_session_mode = current_session_mode

            for control_output in tool_call_control_outputs:
                new_session_mode = control_output.get("mode") or current_session_mode

            if new_session_mode != current_session_mode:
                self._logger.info(
                    f"Changing session {context.session.id} mode to '{new_session_mode}'"
                )

                await self._entity_commands.update_session(
                    session_id=context.session.id,
                    params={
                        "mode": new_session_mode,
                    },
                )

    async def _get_preamble_task(self, context: LoadedContext) -> asyncio.Task[bool]:
        async def preamble_task() -> bool:
            if (
                # Only consider a preamble in the first iteration
                context.state.iterations_completed == 0
                and await self._perceived_performance_policy.is_preamble_required(context)
            ):
                if not await self._hooks.call_on_generating_preamble(context):
                    return False

                await asyncio.sleep(
                    await self._perceived_performance_policy.get_preamble_delay(context),
                )

                if await self._generate_preamble(context):
                    context.interaction = await self._load_interaction_state(context.info)

                await self._emit_ready_event(context)

                if not await self._hooks.call_on_generated_preamble(context):
                    return False
            else:
                pass  # No preamble message is needed

            # Emit a processing event to indicate that the agent is thinking

            await asyncio.sleep(
                await self._perceived_performance_policy.get_processing_indicator_delay(context),
            )

            await self._emit_processing_event(context)

            return True

        return asyncio.create_task(preamble_task())

    async def _generate_preamble(
        self,
        context: LoadedContext,
    ) -> bool:
        generated_messages = False

        for event_generation_result in await self._get_message_composer(
            context.agent
        ).generate_preamble(
            event_emitter=context.session_event_emitter,
            agent=context.agent,
            customer=context.customer,
            context_variables=context.state.context_variables,
            interaction_history=context.interaction.history,
            terms=list(context.state.glossary_terms),
            capabilities=context.state.capabilities,
            ordinary_guideline_matches=context.state.ordinary_guideline_matches,
            tool_enabled_guideline_matches=context.state.tool_enabled_guideline_matches,
            journeys=context.state.journeys,
            tool_insights=context.state.tool_insights,
            staged_events=context.state.tool_events,
        ):
            generated_messages = True
            context.state.message_events += [e for e in event_generation_result.events if e]

        return generated_messages

    async def _generate_messages(
        self,
        context: LoadedContext,
        latch: CancellationSuppressionLatch,
    ) -> Sequence[MessageGenerationInspection]:
        message_generation_inspections = []

        for event_generation_result in await self._get_message_composer(
            context.agent
        ).generate_response(
            event_emitter=context.session_event_emitter,
            agent=context.agent,
            customer=context.customer,
            context_variables=context.state.context_variables,
            interaction_history=context.interaction.history,
            terms=list(context.state.glossary_terms),
            capabilities=context.state.capabilities,
            ordinary_guideline_matches=context.state.ordinary_guideline_matches,
            tool_enabled_guideline_matches=context.state.tool_enabled_guideline_matches,
            journeys=context.state.journeys,
            tool_insights=context.state.tool_insights,
            staged_events=context.state.tool_events,
            latch=latch,
        ):
            context.state.message_events += [e for e in event_generation_result.events if e]

            message_generation_inspections.append(
                MessageGenerationInspection(
                    generations=event_generation_result.generation_info,
                    messages=[
                        e.data.get("message")
                        if e and e.kind == EventKind.MESSAGE and isinstance(e.data, dict)
                        else None
                        for e in event_generation_result.events
                    ],
                )
            )

        return message_generation_inspections

    async def _emit_error_event(self, context: LoadedContext, exception_details: str) -> None:
        await context.session_event_emitter.emit_status_event(
            correlation_id=self._correlator.correlation_id,
            data={
                "status": "error",
                "acknowledged_offset": context.interaction.last_known_event_offset,
                "data": {"exception": exception_details},
            },
        )

    async def _emit_acknowledgement_event(self, context: LoadedContext) -> None:
        await context.session_event_emitter.emit_status_event(
            correlation_id=self._correlator.correlation_id,
            data={
                "acknowledged_offset": context.interaction.last_known_event_offset,
                "status": "acknowledged",
                "data": {},
            },
        )

    async def _emit_processing_event(self, context: LoadedContext) -> None:
        await context.session_event_emitter.emit_status_event(
            correlation_id=self._correlator.correlation_id,
            data={
                "acknowledged_offset": context.interaction.last_known_event_offset,
                "status": "processing",
                "data": {},
            },
        )

    async def _emit_cancellation_event(self, context: LoadedContext) -> None:
        await context.session_event_emitter.emit_status_event(
            correlation_id=self._correlator.correlation_id,
            data={
                "acknowledged_offset": context.interaction.last_known_event_offset,
                "status": "cancelled",
                "data": {},
            },
        )

    async def _emit_ready_event(self, context: LoadedContext) -> None:
        await context.session_event_emitter.emit_status_event(
            correlation_id=self._correlator.correlation_id,
            data={
                "acknowledged_offset": context.interaction.last_known_event_offset,
                "status": "ready",
                "data": {},
            },
        )

    def _get_message_composer(self, agent: Agent) -> MessageEventComposer:
        # Each agent may use a different composition mode,
        # and, moreover, the same agent can change composition
        # modes every now and then. This makes sure that we are
        # composing the message using the right mechanism for this agent.
        match agent.composition_mode:
            case CompositionMode.FLUID:
                return self._fluid_message_generator
            case (
                CompositionMode.STRICT_UTTERANCE
                | CompositionMode.COMPOSITED_UTTERANCE
                | CompositionMode.FLUID_UTTERANCE
            ):
                return self._utterance_selector

        raise Exception("Unsupported agent composition mode")

    async def _load_context_variables(
        self,
        context: LoadedContext,
    ) -> list[tuple[ContextVariable, ContextVariableValue]]:
        variables_supported_by_agent = (
            await self._entity_queries.find_context_variables_for_context(
                agent_id=context.agent.id,
            )
        )

        result = []

        keys_to_check_in_order_of_importance = (
            [context.customer.id]  # Customer-specific value
            + [f"tag:{tag_id}" for tag_id in context.customer.tags]  # Tag-specific value
            + [ContextVariableStore.GLOBAL_KEY]  # Global value
        )

        for variable in variables_supported_by_agent:
            # Try keys in order of importance, stopping at and using
            # the first (and most important) set key for each variable.
            for key in keys_to_check_in_order_of_importance:
                if value := await self._load_context_variable_value(context, variable, key):
                    result.append((variable, value))
                    break

        return result

    async def _capture_tool_preexecution_state(
        self, context: LoadedContext
    ) -> ToolPreexecutionState:
        return await self._tool_event_generator.create_preexecution_state(
            context.session_event_emitter,
            context.session.id,
            context.agent,
            context.customer,
            context.state.context_variables,
            context.interaction.history,
            list(context.state.glossary_terms),
            context.state.ordinary_guideline_matches,
            context.state.tool_enabled_guideline_matches,
            context.state.tool_events,
        )

    async def _load_matched_guidelines_and_journeys(
        self,
        context: LoadedContext,
    ) -> tuple[
        GuidelineMatchingResult,
        list[GuidelineMatch],
        dict[GuidelineMatch, list[ToolId]],
        list[Journey],
    ]:
        # Step 1: Retrieve the journeys likely to be activated for this agent
        relevant_journeys = await self._load_journeys(context)

        # Step 2:
        all_stored_guidelines = {
            g.id: g
            for g in await self._entity_queries.find_guidelines_for_context(
                agent_id=context.agent.id,
                journeys=relevant_journeys,
            )
            if g.enabled
        }

        # Step 3: Exclude guidelines whose prerequisite journeys are less likely to be activated
        # (everything beyond the first journey). Removing these low-probability
        # dependencies up-front keeps the first matching pass fast and focused.
        top_k = 3
        relevant_guidelines = await self._prune_low_probability_journey_guidelines(
            relevant_journeys=relevant_journeys,
            all_stored_guidelines=all_stored_guidelines,
            top_k=top_k,
        )

        # Step 4: Filter the best matches out of those.
        matching_result = await self._guideline_matcher.match_guidelines(  # TODO HERE BAR
            agent=context.agent,
            session=context.session,
            customer=context.customer,
            context_variables=context.state.context_variables,
            interaction_history=context.interaction.history,
            terms=list(context.state.glossary_terms),
            capabilities=context.state.capabilities,
            staged_events=context.state.tool_events,
            guidelines=relevant_guidelines,
        )

        # Step 5: Filter the journeys that are activated by the matched guidelines.
        match_ids = set(map(lambda g: g.guideline.id, matching_result.matches))
        journeys = [j for j in relevant_journeys if set(j.conditions).intersection(match_ids)]

        # Step 6: If any of the lower-probability journeys (those originally filtered out)
        # have in fact been activated, run an additional matching pass for the guidelines
        # that depend on them so we don’t miss relevant behavior.
        if second_match_result := await self._process_activated_low_probability_journey_guidelines(
            context=context,
            all_stored_guidelines=all_stored_guidelines,
            relevant_journeys=relevant_journeys,
            activated_journeys=journeys,
            top_k=top_k,
        ):
            batches = list(chain(matching_result.batches, second_match_result.batches))
            matches = list(chain.from_iterable(batches))

            matching_result = GuidelineMatchingResult(
                total_duration=matching_result.total_duration + second_match_result.total_duration,
                batch_count=matching_result.batch_count + second_match_result.batch_count,
                batch_generations=list(
                    chain(
                        matching_result.batch_generations,
                        second_match_result.batch_generations,
                    )
                ),
                batches=batches,
                matches=matches,
            )

        # Step 7: Resolve guideline matches by loading related guidelines that may not have
        # been inferrable just by looking at the interaction.
        all_relevant_guidelines = await self._relational_guideline_resolver.resolve(
            usable_guidelines=list(all_stored_guidelines.values()),
            matches=matching_result.matches,
            journeys=journeys,
        )

        # Step 8: Distinguish between ordinary and tool-enabled guidelines.
        # We do this here as it creates a better subsequent control flow in the engine.
        tool_enabled_guidelines = await self._find_tool_enabled_guideline_matches(
            guideline_matches=all_relevant_guidelines,
        )

        ordinary_guidelines = list(
            set(all_relevant_guidelines).difference(tool_enabled_guidelines),
        )

        return matching_result, ordinary_guidelines, tool_enabled_guidelines, journeys

    async def _find_tool_enabled_guideline_matches(
        self,
        guideline_matches: Sequence[GuidelineMatch],
    ) -> dict[GuidelineMatch, list[ToolId]]:
        # Create a convenient accessor dict for tool-enabled guidelines (and their tools).
        # This allows for optimized control and data flow in the engine.

        guideline_tool_associations = list(
            await self._entity_queries.find_guideline_tool_associations()
        )
        guideline_matches_by_id = {p.guideline.id: p for p in guideline_matches}

        relevant_associations = [
            a for a in guideline_tool_associations if a.guideline_id in guideline_matches_by_id
        ]

        tools_for_guidelines: dict[GuidelineMatch, list[ToolId]] = defaultdict(list)

        for association in relevant_associations:
            tools_for_guidelines[guideline_matches_by_id[association.guideline_id]].append(
                association.tool_id
            )

        return dict(tools_for_guidelines)

    async def _prune_low_probability_journey_guidelines(
        self,
        relevant_journeys: Sequence[Journey],
        all_stored_guidelines: dict[GuidelineId, Guideline],
        top_k: int,
    ) -> list[Guideline]:
        # Prune low-probability journey-dependent guidelines
        # by only keeping those that are either not dependent on any journey
        # or are dependent on the top K most relevant journeys.
        relevant_journeys_dependent_ids = set(
            chain.from_iterable(
                [
                    await self._entity_queries.find_journey_scoped_guidelines(j)
                    for j in relevant_journeys
                ]
            )
        )

        high_prob_journey_dependent_ids = set(
            chain.from_iterable(
                [
                    await self._entity_queries.find_journey_scoped_guidelines(j)
                    for j in relevant_journeys[:top_k]
                ]
            )
        )

        return [
            g
            for id, g in all_stored_guidelines.items()
            if id in high_prob_journey_dependent_ids or id not in relevant_journeys_dependent_ids
        ]

    async def _process_activated_low_probability_journey_guidelines(
        self,
        context: LoadedContext,
        all_stored_guidelines: dict[GuidelineId, Guideline],
        relevant_journeys: Sequence[Journey],
        activated_journeys: Sequence[Journey],
        top_k: int,
    ) -> Optional[GuidelineMatchingResult]:
        activated_low_priority_dep_ids = set(
            chain.from_iterable(
                [
                    await self._entity_queries.find_journey_scoped_guidelines(j)
                    for j in [
                        activated_journey
                        for activated_journey in activated_journeys
                        if activated_journey in relevant_journeys[top_k:]
                    ]
                ]
            )
        )

        if activated_low_priority_dep_ids:
            self._logger.operation(
                "Second-pass: matching guidelines dependent on activated low-priority journeys"
            )
            additional_matching_guidelines = [
                g for id, g in all_stored_guidelines.items() if id in activated_low_priority_dep_ids
            ]
            return await self._guideline_matcher.match_guidelines(
                agent=context.agent,
                session=context.session,
                customer=context.customer,
                context_variables=context.state.context_variables,
                interaction_history=context.interaction.history,
                terms=list(context.state.glossary_terms),
                capabilities=context.state.capabilities,
                staged_events=context.state.tool_events,
                guidelines=additional_matching_guidelines,
            )

        return None

    async def _load_capabilities(self, context: LoadedContext) -> Sequence[Capability]:
        # Capabilities are retrieved using semantic similarity.
        # The querying process is done with a text query, for which
        # the K most relevant terms are retrieved.
        #
        # We thus build an optimized query here based on our context.
        query = ""

        if context.interaction.history:
            query += str([e.data for e in context.interaction.history])

        if query:
            return await self._entity_queries.find_capabilities_for_agent(
                agent_id=context.agent.id,
                query=query,
                max_count=3,
            )

        return []

    async def _load_glossary_terms(self, context: LoadedContext) -> Sequence[Term]:
        # Glossary terms are retrieved using semantic similarity.
        # The querying process is done with a text query, for which
        # the K most relevant terms are retrieved.
        #
        # We thus build an optimized query here based on our context and state.
        query = ""

        if context.state.context_variables:
            query += f"\n{context_variables_to_json(context.state.context_variables)}"

        if context.interaction.history:
            query += str([e.data for e in context.interaction.history])

        if context.state.guidelines:
            query += str(
                [
                    f"When {g.content.condition}, then {g.content.action}"
                    if g.content.action
                    else f"When {g.content.condition}"
                    for g in context.state.guidelines
                ]
            )

        if context.state.tool_events:
            query += str([e.data for e in context.state.tool_events])

        if query:
            return await self._entity_queries.find_glossary_terms_for_context(
                agent_id=context.agent.id,
                query=query,
            )

        return []

    async def _load_journeys(
        self,
        context: LoadedContext,
    ) -> Sequence[Journey]:
        # Journeys are retrieved using semantic similarity.
        # The querying process is done with a text query, for which
        # the K most relevant terms are retrieved.
        #
        # We thus build an optimized query here based on our context and state.
        all_journeys = await self._entity_queries.finds_journeys_for_context(
            agent_id=context.agent.id,
        )

        query = ""

        if context.state.context_variables:
            query += f"\n{context_variables_to_json(context.state.context_variables)}"

        if context.state.guidelines:
            query += str(
                [
                    f"When {g.content.condition}, then {g.content.action}"
                    if g.content.action
                    else f"When {g.content.condition}"
                    for g in context.state.guidelines
                ]
            )

        if context.state.all_events:
            query += str([e.data for e in context.state.all_events])

        if context.state.glossary_terms:
            query += str([t.name for t in context.state.glossary_terms])

        if context.interaction.history:
            query += str([e.data for e in context.interaction.history])

        if query:
            return await self._entity_queries.find_relevant_journeys_for_context(
                available_journeys=all_journeys,
                query=query,
            )

        return []

    async def _call_tools(
        self,
        context: LoadedContext,
        preexecution_state: ToolPreexecutionState,
    ) -> tuple[ToolEventGenerationResult, list[EmittedEvent], ToolInsights] | None:
        result = await self._tool_event_generator.generate_events(
            preexecution_state,
            session_event_emitter=context.session_event_emitter,
            response_event_emitter=context.response_event_emitter,
            session_id=context.session.id,
            agent=context.agent,
            customer=context.customer,
            context_variables=context.state.context_variables,
            interaction_history=context.interaction.history,
            terms=list(context.state.glossary_terms),
            ordinary_guideline_matches=context.state.ordinary_guideline_matches,
            tool_enabled_guideline_matches=context.state.tool_enabled_guideline_matches,
            journeys=context.state.journeys,
            staged_events=context.state.tool_events,
        )

        tool_events = [e for e in result.events if e] if result else []

        return result, tool_events, result.insights

    async def _utterance_requests_to_guideline_matches(
        self,
        requests: Sequence[UtteranceRequest],
    ) -> Sequence[GuidelineMatch]:
        # Utterance requests are reduced to guidelines, to take advantage
        # of the engine's ability to consistently adhere to guidelines.

        def utterance_to_match(i: int, utterance: UtteranceRequest) -> GuidelineMatch:
            rationales = {
                UtteranceReason.BUY_TIME: "An external module has determined that this response is necessary, and you must adhere to it.",
                UtteranceReason.FOLLOW_UP: "An external module has determined that this response is necessary, and you must adhere to it.",
            }

            conditions = {
                UtteranceReason.BUY_TIME: "-- RIGHT NOW!",
                UtteranceReason.FOLLOW_UP: "-- RIGHT NOW!",
            }

            return GuidelineMatch(
                guideline=Guideline(
                    id=GuidelineId(f"<utterance-request-{i}>"),
                    creation_utc=datetime.now(timezone.utc),
                    content=GuidelineContent(
                        condition=conditions[utterance.reason],
                        action=utterance.action,
                    ),
                    enabled=True,
                    tags=[],
                    metadata={},
                ),
                rationale=rationales[utterance.reason],
                score=10,
            )

        return [utterance_to_match(i, request) for i, request in enumerate(requests, start=1)]

    async def _load_context_variable_value(
        self,
        context: LoadedContext,
        variable: ContextVariable,
        key: str,
    ) -> Optional[ContextVariableValue]:
        return await load_fresh_context_variable_value(
            entity_queries=self._entity_queries,
            entity_commands=self._entity_commands,
            agent_id=context.agent.id,
            session=context.session,
            variable=variable,
            key=key,
        )

    async def _filter_problematic_tool_parameters(
        self, problematic_parameters: Sequence[ProblematicToolData]
    ) -> Sequence[ProblematicToolData]:
        precedence_values = [
            m.precedence for m in problematic_parameters if m.precedence is not None
        ]

        if precedence_values == []:
            return problematic_parameters

        return [m for m in problematic_parameters if m.precedence == min(precedence_values)]

    def _todo_add_associated_guidelines(self, guideline_matches: Sequence[GuidelineMatch]) -> None:
        # TODO write this method - it should add guidelines that are associated with the previously matched guidelines (due to having similar actions, as flagged by the conversation designer)
        return

    async def _add_agent_state(
        self,
        context: LoadedContext,
        session: Session,
        guideline_matches: Sequence[GuidelineMatch],
    ) -> None:
        matches_to_analyze = [
            match
            for match in guideline_matches
            if match.guideline.id not in session.agent_state["applied_guideline_ids"]
            and not match.guideline.metadata.get("continuous", False)
            and match.guideline.content.action
        ]

        self._todo_add_associated_guidelines(matches_to_analyze)

        result = await self._guideline_matcher.analyze_response(
            agent=context.agent,
            session=session,
            customer=context.customer,
            context_variables=context.state.context_variables,
            interaction_history=context.interaction.history,
            terms=list(context.state.glossary_terms),
            staged_events=context.state.tool_events,
            guideline_matches=matches_to_analyze,
        )

        applied_guideline_ids = [
            a.guideline.id for a in result.analyzed_guidelines if a.is_previously_applied
        ]

        applied_guideline_ids.extend(session.agent_state["applied_guideline_ids"])

        await self._entity_commands.update_session(
            session_id=session.id,
            params=SessionUpdateParams(
                agent_state=AgentState(applied_guideline_ids=applied_guideline_ids)
            ),
        )


# This is module-level and public for isolated testability purposes.
async def load_fresh_context_variable_value(
    entity_queries: EntityQueries,
    entity_commands: EntityCommands,
    agent_id: AgentId,
    session: Session,
    variable: ContextVariable,
    key: str,
    current_time: datetime = datetime.now(timezone.utc),
) -> Optional[ContextVariableValue]:
    # Load the existing value
    value = await entity_queries.read_context_variable_value(
        variable_id=variable.id,
        key=key,
    )

    # If there's no tool attached to this variable,
    # return the value we found for the key.
    # Note that this may be None here, which is okay.
    if not variable.tool_id:
        return value

    # So we do have a tool attached.
    # Do we already have a value, and is it sufficiently fresh?
    if value and variable.freshness_rules:
        cron_iterator = croniter(variable.freshness_rules, value.last_modified)

        if cron_iterator.get_next(datetime) > current_time:
            # We already have a fresh value in store. Return it.
            return value

    # We don't have a sufficiently fresh value.
    # Get an updated one, utilizing the associated tool.

    tool_context = ToolContext(
        agent_id=agent_id,
        session_id=session.id,
        customer_id=session.customer_id,
    )

    tool_service = await entity_queries.read_tool_service(variable.tool_id.service_name)

    tool_result = await tool_service.call_tool(
        variable.tool_id.tool_name,
        context=tool_context,
        arguments={},
    )

    return await entity_commands.update_context_variable_value(
        variable_id=variable.id,
        key=key,
        data=tool_result.data,
    )
