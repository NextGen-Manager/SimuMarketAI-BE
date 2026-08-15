"""Turn raw council output into typed artifacts.

Free prose never becomes an input to scoring. Each council emits schema-valid
ballots, and the reducers here count them. Counting is arithmetic the adapter
does, not the model: a distribution the LLM reports about itself would be a
number produced by an LLM, which ADR-001 does not allow.

The metrics are counts rather than shares. A share would be a ratio presented
next to real market data, and docs/04 is explicit that synthetic purchase share
is not a conversion rate; showing "9 dari 16 persona" keeps that visible.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.agents import (
    ARCHETYPE_LABELS,
    CustomerSimulationResult,
    ObjectionTally,
    PriceBand,
    SegmentTally,
    SyntheticQuote,
)

SIMULATION_LIMITATIONS: tuple[str, ...] = (
    "Respons persona adalah sinyal sintetis eksploratif, bukan perilaku pelanggan nyata.",
    "Cohort tidak dibobot terhadap populasi sehingga distribusi tidak mewakili pasar.",
    "Angka pada bagian ini tidak dipakai sebagai input skor kelayakan.",
)


class PersonaBallot(BaseModel):
    """One persona's structured ballot, exactly as docs/03 specifies.

    Neither reaction nor opinion shift is a field here. A persona reporting "I
    reacted" or "I changed my mind" would be an LLM producing a number that ends
    up in the report; both are instead derived by `reduce_persona_ballots` from
    the round trace and from comparing the baseline ballot with the final one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    archetype: str = Field(min_length=1, max_length=64)
    choice: Literal["purchase", "consider", "reject"]
    objection_code: str = Field(min_length=1, max_length=64)
    objection_label: str = Field(min_length=1, max_length=200)
    acceptable_price_min_idr: int = Field(ge=0)
    acceptable_price_max_idr: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def validate_band(self) -> PersonaBallot:
        if self.acceptable_price_min_idr > self.acceptable_price_max_idr:
            raise ValueError("acceptable_price_min_idr melebihi acceptable_price_max_idr")
        return self


def _median(values: Sequence[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    # Integer rupiah all the way: the midpoint of two integers is averaged with
    # integer division rather than becoming a float.
    return (ordered[middle - 1] + ordered[middle]) // 2


def reduce_persona_ballots(
    ballots: Sequence[PersonaBallot],
    *,
    cohort_version: str,
    cohort_size: int,
    rounds: int,
    baseline: Sequence[PersonaBallot] = (),
    reactions: Mapping[str, int] | None = None,
    social_exposure_complete: bool = True,
    max_quotes: int = 4,
) -> CustomerSimulationResult:
    """Count the final ballots, the observed reactions, and the opinion shifts.

    `ballots` are the final ballots, `baseline` the private round-0 ones, and
    `reactions` the positive actions observed in the trace per agent. Opinion
    shift is a comparison we make between two ballots, and positive reaction is
    a count of actions OASIS recorded — neither is a model's claim about itself.
    """
    observed = reactions or {}
    baseline_choice = {ballot.agent_id: ballot.choice for ballot in baseline}

    activated = len(ballots)
    purchase = sum(1 for ballot in ballots if ballot.choice == "purchase")
    positive = sum(1 for ballot in ballots if observed.get(ballot.agent_id, 0) > 0)
    shifted = sum(
        1
        for ballot in ballots
        if ballot.agent_id in baseline_choice and baseline_choice[ballot.agent_id] != ballot.choice
    )

    per_archetype: Counter[str] = Counter(ballot.archetype for ballot in ballots)
    purchase_per_archetype: Counter[str] = Counter(
        ballot.archetype for ballot in ballots if ballot.choice == "purchase"
    )
    segments = [
        SegmentTally(
            archetype=archetype,
            persona_count=count,
            purchase_intent_count=purchase_per_archetype.get(archetype, 0),
        )
        for archetype, count in sorted(per_archetype.items())
    ]

    labels = {ballot.objection_code: ballot.objection_label for ballot in ballots}
    objection_counts: Counter[str] = Counter(ballot.objection_code for ballot in ballots)
    objections = [
        ObjectionTally(code=code, label=labels[code], count=count)
        for code, count in sorted(objection_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    band: PriceBand | None = None
    if ballots:
        band = PriceBand(
            min_idr=_median([ballot.acceptable_price_min_idr for ballot in ballots]),
            max_idr=_median([ballot.acceptable_price_max_idr for ballot in ballots]),
        )

    quotes = [
        SyntheticQuote(
            agent_id=ballot.agent_id,
            archetype=ARCHETYPE_LABELS.get(ballot.archetype, ballot.archetype),
            text=ballot.quote,
        )
        for ballot in ballots[:max_quotes]
    ]

    limitations = list(SIMULATION_LIMITATIONS)
    if not social_exposure_complete:
        limitations.append(
            "Tidak seluruh persona menerima stimulus melalui feed sosial; "
            "reaksi hanya dihitung dari exposure yang terverifikasi."
        )

    return CustomerSimulationResult(
        cohort_version=cohort_version,
        cohort_size=cohort_size,
        rounds=rounds,
        activated_persona_count=activated,
        purchase_intent_count=purchase,
        positive_reaction_count=positive,
        opinion_shift_count=shifted,
        segments=segments,
        objections=objections,
        acceptable_price_band=band,
        quotes=quotes,
        limitations=limitations,
    )
