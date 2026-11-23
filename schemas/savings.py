"""Strict browser contracts: provider identifiers are never accepted."""
from datetime import date
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

Kobo = Annotated[int, Field(strict=True, gt=0, le=9007199254740991)]


class SavingsGoalCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    target_amount_kobo: Kobo
    target_date: date
    interest_enabled: StrictBool

    @field_validator('target_date', mode='before')
    @classmethod
    def date_only(cls, value):
        if isinstance(value, str):
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError('Use YYYY-MM-DD')
        elif type(value) is not date:
            raise ValueError('Use YYYY-MM-DD')
        return value


class SavingsFund(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount_kobo: Kobo


class SavingsAccountCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone_number: str = Field(min_length=7, max_length=40)
    terms_of_use_accepted: Literal[True]


class SavingsIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bvn: str = Field(pattern=r'^\d{11}$')
