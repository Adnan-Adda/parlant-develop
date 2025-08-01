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

# This is a separate module to avoid circular dependencies

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from parlant.core.common import JSONSerializable
from parlant.core.guidelines import Guideline


class PreviouslyAppliedType(Enum):
    NO = "no"
    PARTIALLY = "partially"
    FULLY = "fully"
    IRRELEVANT = "irrelevant"


@dataclass(frozen=True)
class GuidelineMatch:
    guideline: Guideline
    score: int
    rationale: str
    guideline_previously_applied: PreviouslyAppliedType = PreviouslyAppliedType.NO
    metadata: Mapping[str, JSONSerializable] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(f"{self.guideline.id}_{self.score}_{self.rationale}")


@dataclass(frozen=True)
class AnalyzedGuideline:
    guideline: Guideline
    is_previously_applied: bool
