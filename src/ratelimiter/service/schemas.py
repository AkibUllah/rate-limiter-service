from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, model_validator

from ..policy import Algorithm, Policy

NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$"
MAX_POLICIES_PER_CHECK = 16


class PolicySpec(BaseModel):
    limit: int = Field(gt=0, description="Requests allowed per window")
    window_seconds: float = Field(gt=0)
    algorithm: Algorithm = Algorithm.SLIDING_COUNTER
    burst: int | None = Field(default=None, gt=0, description="Token bucket / GCRA burst capacity")

    def to_policy(self, name: str | None = None) -> Policy:
        return Policy(
            limit=self.limit,
            window=self.window_seconds,
            algorithm=self.algorithm,
            burst=self.burst,
            name=name,
        )


class PolicyIn(PolicySpec):
    name: str = Field(pattern=NAME_PATTERN)


class PolicyOut(BaseModel):
    name: str
    limit: int
    window_seconds: float
    algorithm: Algorithm
    burst: int | None
    rate_per_second: float

    @classmethod
    def from_policy(cls, policy: Policy) -> PolicyOut:
        return cls(
            name=policy.name or "",
            limit=policy.limit,
            window_seconds=policy.window,
            algorithm=policy.algorithm,
            burst=policy.burst,
            rate_per_second=policy.rate,
        )


class CheckIn(BaseModel):
    key: str = Field(
        min_length=1, max_length=256, description="What is being limited: user, IP, tenant..."
    )
    policy: str | None = Field(default=None, description="Name of a stored policy")
    inline: PolicySpec | None = Field(
        default=None, description="Ad-hoc policy used when no name is given"
    )
    policies: list[Annotated[str, StringConstraints(pattern=NAME_PATTERN)]] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_POLICIES_PER_CHECK,
        description="Stored policies to check together: all allow and consume, or none consume",
    )
    cost: int = Field(default=1, gt=0, le=10_000)

    @model_validator(mode="after")
    def one_policy_source(self) -> CheckIn:
        sources = [self.policy, self.inline, self.policies]
        if sum(bool(source) for source in sources) > 1:
            raise ValueError("provide only one of `policy`, `inline` or `policies`")
        if self.policies is not None and len(set(self.policies)) != len(self.policies):
            raise ValueError("`policies` must not repeat a name")
        return self


class PolicyResultOut(BaseModel):
    policy: str
    allowed: bool = Field(description="Whether this policy alone would let the request through")
    limit: int
    remaining: int
    retry_after: float
    reset_after: float
    algorithm: Algorithm


class CheckOut(BaseModel):
    allowed: bool
    limit: int
    remaining: int
    retry_after: float
    reset_after: float
    key: str
    policy: str = Field(description="The policy checked, or the most restrictive of `policies`")
    algorithm: Algorithm
    policies: list[PolicyResultOut] | None = Field(
        default=None, description="Per-policy results, for a check of several policies"
    )


class InspectOut(BaseModel):
    would_allow: bool = Field(description="Whether a request of `cost` units would pass now")
    limit: int
    remaining: int = Field(description="Units left before that request")
    retry_after: float = Field(description="Seconds until a request of `cost` units would pass")
    reset_after: float = Field(description="Seconds until the key's budget is full again")
    key: str
    policy: str
    algorithm: Algorithm
    cost: int


class ResetIn(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    policy: str = Field(description="Name of the stored policy whose state should be cleared")


class AlgorithmOut(BaseModel):
    name: Algorithm
    description: str


class HealthOut(BaseModel):
    status: str
    version: str
    backend: str
    policies: int


class MessageOut(BaseModel):
    message: str
