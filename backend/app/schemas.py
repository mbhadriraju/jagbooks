from datetime import date as Date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator
from app.models import CATEGORIES

FinancialClass = Literal["revenue", "expense", "asset", "liability", "cash_flow", "purchase_order"]
MAX_AMOUNT_CENTS = 100_000_000_000
ExtractedAmount = Annotated[
    Decimal,
    Field(ge=-MAX_AMOUNT_CENTS // 100, le=MAX_AMOUNT_CENTS // 100, decimal_places=2),
    WithJsonSchema({"type": "number"}),
]
ExtractedCategory = Annotated[
    str,
    WithJsonSchema({"type": "string", "enum": [category for categories in CATEGORIES.values() for category in categories]}),
]


class SheetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: UUID
    name: str = Field(min_length=1, max_length=160, pattern=r".*\S.*")


class TransactionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str | None = Field(default=None, max_length=160)
    date: Date | None = None
    description: str | None = Field(default=None, min_length=1, max_length=3000, pattern=r".*\S.*")
    amount_cents: int | None = Field(default=None, strict=True, ge=-MAX_AMOUNT_CENTS, le=MAX_AMOUNT_CENTS)
    financial_class: FinancialClass | None = None
    category: str | None = None
    review_status: Literal["draft", "approved", "rejected"] = "draft"

    @model_validator(mode="after")
    def validate_classification(self):
        if self.category is not None and self.category not in CATEGORIES.get(self.financial_class, []):
            raise ValueError("Category must belong to financial_class.")
        if self.review_status == "approved" and any(x is None for x in (self.date, self.description, self.amount_cents, self.financial_class, self.category)):
            raise ValueError("Approval requires date, description, amount, class, and category.")
        return self


class ExtractedTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transaction_id: str | None
    amount: ExtractedAmount | None
    date: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    description: str | None
    financial_class: FinancialClass | None
    category: ExtractedCategory | None

    def to_draft(self) -> TransactionInput:
        return TransactionInput(
            reference=self.transaction_id,
            amount_cents=None if self.amount is None else int(self.amount * 100),
            date=None if self.date is None else Date.fromisoformat(self.date),
            description=self.description,
            financial_class=self.financial_class,
            category=self.category,
        )


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transactions: list[ExtractedTransaction] = Field(max_length=2000)


class RestoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version_id: UUID
