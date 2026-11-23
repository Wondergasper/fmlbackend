from enum import Enum
from typing import Literal
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class BuyerMetadata(BaseModel):
    account_type: Literal['individual', 'business'] = 'individual'
    business_name: str | None = Field(default=None, min_length=1, max_length=200)
    business_phone: str | None = Field(default=None, min_length=3, max_length=40)

    @field_validator('business_name', 'business_phone', mode='before')
    @classmethod
    def trim_business_fields(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def validate_business(self):
        if self.account_type == 'business':
            if getattr(self, 'role', 'customer') != 'customer':
                raise ValueError('Business accounts must have the customer role.')
            if not self.business_name or not self.business_phone:
                raise ValueError('Business name and phone are required.')
        elif self.business_name is not None or self.business_phone is not None:
            raise ValueError('Business metadata requires a business account.')
        return self


class Role(str, Enum):
    CUSTOMER = "customer"
    VENDOR   = "vendor"
    ADMIN    = "admin"
    LOGIST   = "logist"
    RIDER    = "rider"
    # Backward compatibility aliases
    DRIVER   = "driver"
    COURIER  = "courier"


ROLE_ALIASES: dict[str, str] = {
    "driver": Role.RIDER.value,
    "courier": Role.RIDER.value,
    "logistics": Role.LOGIST.value,
    "superadmin": Role.ADMIN.value,
}


def canonicalize_role(role: str | None) -> str | None:
    """Normalize a role string or alias to its canonical representation."""
    if not role:
        return None
    r = role.strip().lower()
    return ROLE_ALIASES.get(r, r)


# Canonical role groupings for permission dependencies
ROLE_GROUP_DELIVERY: set[str] = {Role.RIDER.value, Role.DRIVER.value, Role.COURIER.value}
ROLE_GROUP_DISPATCH: set[str] = {Role.ADMIN.value, Role.LOGIST.value}
ROLE_GROUP_LOGISTICS: set[str] = ROLE_GROUP_DELIVERY | ROLE_GROUP_DISPATCH
ROLE_GROUP_MERCHANT: set[str] = {Role.VENDOR.value, Role.ADMIN.value}
ALL_PLATFORM_ROLES: set[str] = {
    Role.CUSTOMER.value,
    Role.VENDOR.value,
    Role.ADMIN.value,
    Role.LOGIST.value,
    Role.RIDER.value,
    Role.DRIVER.value,
    Role.COURIER.value,
}


class RegisterRequest(BuyerMetadata):
    email: str
    password: str
    full_name: str
    role: str = "customer"
    phone: str | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        r = canonicalize_role(v)
        if not r or r not in {Role.CUSTOMER.value, Role.VENDOR.value, Role.ADMIN.value, Role.LOGIST.value, Role.RIDER.value}:
            raise ValueError(f"Invalid role '{v}'. Must be one of {sorted(ALL_PLATFORM_ROLES)}")
        return v.lower()


class RegisterResponse(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    roles: list[str]
    message: str = "Account created successfully."


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    user: dict


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    phone: str | None = None
    role: str
    roles: list[str]
    created_at: str | None = None
    updated_at: str | None = None


class SendOtpRequest(BaseModel):
    email: EmailStr


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp_code: str


class GoogleAuthRequest(BaseModel):
    id_token: str
    role: str = "customer"
