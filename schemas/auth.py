from enum import Enum
from pydantic import BaseModel, EmailStr, field_validator


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


class RegisterRequest(BaseModel):
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
