from pydantic import BaseModel
from typing import Optional


class PlatformConfigUpdate(BaseModel):
    platform_fees: Optional[int] = None
    delivery_base_fee: Optional[int] = None
    delivery_express_fee: Optional[int] = None
    gateway_mode: Optional[str] = None
    signup_bonus: Optional[int] = None
    farmer_rewards_rate: Optional[int] = None


class CategoryCreate(BaseModel):
    name: str


class CategoryDelete(BaseModel):
    name: str
