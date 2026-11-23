"""ApiResponse envelope — carries data plus non-fatal warnings so integration
hiccups surface instead of being swallowed (docs/design/08-api.md §1, golden rule #3)."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Warning_(BaseModel):
    """A non-fatal issue surfaced to the caller (named Warning_ to avoid shadowing
    the builtin)."""

    code: str
    message: str
    details: dict | None = None


class ApiResponse(BaseModel, Generic[T]):
    data: T | None = None
    warnings: list[Warning_] = Field(default_factory=list)
    meta: dict | None = None

    @classmethod
    def ok(cls, data: T, *, meta: dict | None = None) -> ApiResponse[T]:
        return cls(data=data, meta=meta)

    @classmethod
    def with_warnings(
        cls, data: T, warnings: list[Warning_], *, meta: dict | None = None
    ) -> ApiResponse[T]:
        return cls(data=data, warnings=warnings, meta=meta)
