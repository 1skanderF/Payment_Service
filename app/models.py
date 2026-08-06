from pydantic import BaseModel, field_validator
from decimal import Decimal, InvalidOperation


class OperationCreate(BaseModel):
    operationId: str
    amount: str
    currency: str = "RUB"
    description: str

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: str) -> str:
        try:
            d = Decimal(v)
        except InvalidOperation:
            raise ValueError("amount must be a valid decimal string")

        if d <= 0:
            raise ValueError("amount must be positive")

        # Проверяем, что не больше 2 знаков после точки
        if -d.as_tuple().exponent > 2:
            raise ValueError("amount must have at most 2 decimal places")

        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        # По заданию только RUB
        if v != "RUB":
            raise ValueError("only RUB is supported")
        return v


class Receipt(BaseModel):
    providerPaymentId: str
    operationId: str
    result: str
    message: str
    occurredAt: str