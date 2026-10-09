from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

AssistantPage = Literal[
    "dashboard",
    "invoices",
    "collections",
    "inventory",
    "analytics",
    "expenses",
    "tax",
    "settings",
    "billing",
]


class WebAssistantQuestion(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    message: str = Field(min_length=1, max_length=500)
    page: AssistantPage = "dashboard"
    allow_ai: bool = True


class AssistantInvoiceLine(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    quantity: Literal[1] = 1
    unit_price: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)


class AssistantInvoiceDraft(BaseModel):
    customer_name: str | None = Field(default=None, max_length=100)
    currency: Literal["NGN", "USD"] = "NGN"
    lines: list[AssistantInvoiceLine] = Field(default_factory=list, max_length=1)


class AssistantNavigationAction(BaseModel):
    kind: Literal["navigate"] = "navigate"
    id: str
    title: str
    description: str
    href: str = Field(pattern=r"^/dashboard(?:[/?#]|$)")


class AssistantDraftAction(BaseModel):
    kind: Literal["invoice_draft"] = "invoice_draft"
    id: Literal["new_invoice"] = "new_invoice"
    title: str = "Review invoice draft"
    description: str
    draft: AssistantInvoiceDraft


AssistantAction = Annotated[AssistantNavigationAction | AssistantDraftAction, Field(discriminator="kind")]


class WebAssistantOut(BaseModel):
    message: str
    actions: list[AssistantAction]
    ai_assisted: bool = False
    notice: str | None = None
