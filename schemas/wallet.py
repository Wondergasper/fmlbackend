from pydantic import BaseModel


class BalanceResponse(BaseModel):
    balance_kobo: int
    balance_naira: float
    formatted: str


class TopupRequest(BaseModel):
    amount_kobo: int
    reference: str


class TopupResponse(BaseModel):
    new_balance_kobo: int
    message: str = "Wallet topped up successfully."


class PayoutRequest(BaseModel):
    amount_kobo: int
    reference: str


class TransactionResponse(BaseModel):
    id: str
    type: str
    amount_kobo: int
    created_at: str | None = None
    status: str
    reference: str
    description: str | None = None


class PaystackInitRequest(BaseModel):
    amount_kobo: int


class PaystackVerifyRequest(BaseModel):
    amount_kobo: int
    reference: str
