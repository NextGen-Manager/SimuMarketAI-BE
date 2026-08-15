"""The narrow surface the round protocol needs from a social runtime.

The protocol in docs/04 is a sequence of social operations: publish a stimulus,
let a subset of agents act on it, let them see each other, ask one agent
privately. `CouncilRuntime` is exactly those operations and nothing else.

Splitting it out is what makes the protocol testable. `CouncilOrchestrator`
implements docs/04 against this port, so the round structure, activation
policy, budget enforcement, and opinion-shift arithmetic are exercised in CI by
a deterministic runtime, while only the binding to `camel-oasis` remains
unverified. Before this split the round design existed on paper and the code ran
one flat pass of chat completions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from app.domain.agents import StrictModel

# What OASIS calls an action, as it appears in the round record. Kept as plain
# strings so this module stays importable without `camel-oasis` installed.
ACTION_CREATE_COMMENT = "create_comment"
ACTION_LIKE_POST = "like_post"
ACTION_DISLIKE_POST = "dislike_post"
ACTION_PURCHASE_PRODUCT = "purchase_product"
ACTION_DO_NOTHING = "do_nothing"

PERSONA_ACTION_SPACE: tuple[str, ...] = (
    ACTION_CREATE_COMMENT,
    ACTION_LIKE_POST,
    ACTION_DISLIKE_POST,
    ACTION_PURCHASE_PRODUCT,
    ACTION_DO_NOTHING,
)

# docs/04: "Positive reaction share | Like / exposure valid". A comment is not a
# positive reaction — it can be an objection — and a purchase is counted
# separately as purchase intent, so neither is folded in here.
POSITIVE_ACTIONS: frozenset[str] = frozenset({ACTION_LIKE_POST})


class AgentReply(StrictModel):
    """One orchestrator-driven reply, plus what it cost.

    `tokens` is per reply, not cumulative. A cumulative figure written onto each
    instance makes later instances look more expensive than they were and
    destroys the per-instance cost signal the budget exists to protect.
    """

    content: str
    tokens: int = 0


class SocialActionResult(StrictModel):
    """One observed social action and the resources used to choose it."""

    action: str
    tokens: int = 0
    duration_ms: int = 0


class CouncilRuntime(Protocol):
    """A social simulation environment for exactly one analysis run."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def restrict_actions(self, agent_index: int, actions: Sequence[str]) -> None:
        """Narrow one agent's action space to its council's allowlist.

        docs/04 gives each council a different allowlist. Applying one action
        space to the whole graph would let a Finance agent take a persona action,
        which is the sort of thing that only shows up as noise in the trace.
        """
        ...

    async def interview(
        self,
        agent_index: int,
        prompt: str,
        *,
        round_index: int,
        purpose: str,
    ) -> AgentReply:
        """Ask one agent directly, driven by the orchestrator.

        This is the `INTERVIEW` action of docs/04, deliberately kept out of
        `available_actions` so no agent can select it for itself.
        """
        ...

    async def publish_stimulus(
        self,
        payload: Mapping[str, object],
        *,
        round_index: int,
        label: str,
    ) -> None:
        """Post the concept card, or an intervention variant, to the feed."""
        ...

    async def step(
        self,
        agent_indices: Sequence[int],
        *,
        round_index: int,
    ) -> Mapping[int, SocialActionResult]:
        """Advance a round and return each action with its measured usage."""
        ...
