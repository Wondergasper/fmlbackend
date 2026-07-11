from pydantic import BaseModel, EmailStr, field_validator


class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str
    role: str = "customer"
    phone: str | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"customer", "vendor", "admin"}
        if v.lower() not in allowed:
            raise ValueError(f"Invalid role '{v}'. Must be one of {allowed}")
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
