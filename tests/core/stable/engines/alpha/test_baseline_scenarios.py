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

from pytest_bdd import scenarios

from tests.core.common.engines.alpha.utils import load_steps


load_steps(
    "agents",
    "context_variables",
    "engines",
    "events",
    "guidelines",
    "utterances",
    "sessions",
    "terms",
    "tools",
    "customers",
    "tags",
    "journeys",
    "capabilities",
)

scenarios(
    *(
        f"core/stable/engines/alpha/features/baseline/{feature}.feature"
        for feature in (
            "strict_utterances",
            "fluid_utterances",
            "conversation",
            "errors",
            "relationships",
            "moderation",
            "proactivity",
            "supervision",
            "glossary",
            "tools",
            "context_variables",
            "triggered_utterances",
            "journeys",
            "capabilities",
            "strict_utterances_capabilities",
        )
    )
)
