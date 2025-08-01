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
from dataclasses import dataclass
from typing import Sequence, cast

from parlant.core.agents import Agent
from parlant.core.capabilities import Capability
from parlant.core.common import JSONSerializable
from parlant.core.context_variables import ContextVariable, ContextVariableValue
from parlant.core.customers import Customer
from parlant.core.emissions import EmittedEvent, EventEmitter
from parlant.core.engines.alpha.guideline_matching.guideline_match import GuidelineMatch
from parlant.core.engines.types import Context
from parlant.core.engines.alpha.tool_calling.tool_caller import ToolInsights
from parlant.core.glossary import Term
from parlant.core.guidelines import Guideline
from parlant.core.journeys import Journey
from parlant.core.loggers import Logger
from parlant.core.sessions import Event, EventKind, EventSource, Session, ToolEventData
from parlant.core.tools import ToolId, ToolResult


@dataclass(frozen=True)
class Interaction:
    """Helper class to access a session's interaction state"""

    @staticmethod
    def empty() -> Interaction:
        """Returns an empty interaction state"""
        return Interaction([], -1)

    history: Sequence[Event]
    """An sequenced event-by-event representation of the interaction"""

    last_known_event_offset: int
    """An accessor which is often useful when emitting status events"""


@dataclass(frozen=False)
class ResponseState:
    """Used to access and update the state needed for responding properly"""

    context_variables: list[tuple[ContextVariable, ContextVariableValue]]
    glossary_terms: set[Term]
    capabilities: list[Capability]
    ordinary_guideline_matches: list[GuidelineMatch]
    tool_enabled_guideline_matches: dict[GuidelineMatch, list[ToolId]]
    journeys: list[Journey]
    tool_events: list[EmittedEvent]
    tool_insights: ToolInsights
    iterations_completed: int
    prepared_to_respond: bool
    message_events: list[EmittedEvent]

    @property
    def ordinary_guidelines(self) -> list[Guideline]:
        return [gp.guideline for gp in self.ordinary_guideline_matches]

    @property
    def tool_enabled_guidelines(self) -> list[Guideline]:
        return [gp.guideline for gp in self.tool_enabled_guideline_matches.keys()]

    @property
    def guidelines(self) -> list[Guideline]:
        return self.ordinary_guidelines + self.tool_enabled_guidelines

    @property
    def all_events(self) -> list[EmittedEvent]:
        return self.tool_events + self.message_events


@dataclass
class LoadedContext:
    """Helper class to access loaded values that are relevant for responding in a particular context"""

    info: Context
    """The raw call context which is here represented in its loaded form"""

    logger: Logger
    """The logger used to log messages in the current context"""

    correlation_id: str
    """The correlation ID for the current context"""

    agent: Agent
    """The agent which is currently requested to respond"""

    customer: Customer
    """The customer to which the agent is responding"""

    session: Session
    """The session being processed"""

    session_event_emitter: EventEmitter
    """Emits new events into the loaded session"""

    response_event_emitter: EventEmitter
    """Emits new events that are scoped to the current response"""

    interaction: Interaction
    """A snapshot of the interaction history in the loaded session"""

    state: ResponseState
    """The current state of the response being processed"""

    async def add_tool_event(
        self,
        tool_id: ToolId,
        arguments: dict[str, JSONSerializable],
        result: ToolResult,
    ) -> None:
        """Adds a staged tool event to the loaded context"""
        self.state.tool_events.append(
            EmittedEvent(
                source=EventSource.SYSTEM,
                kind=EventKind.TOOL,
                correlation_id=self.correlation_id,
                data=cast(
                    JSONSerializable,
                    ToolEventData(
                        # TODO: Add a common method to create a session-store comptatible ToolCall from ToolResult
                        tool_calls=[
                            {
                                "tool_id": tool_id.to_string(),
                                "arguments": arguments,
                                "result": {
                                    "data": result.data,
                                    "metadata": result.metadata,
                                    "control": result.control,
                                    "utterances": result.utterances,
                                    "utterance_fields": result.utterance_fields,
                                },
                            }
                        ]
                    ),
                ),
            )
        )
