from pydantic import BaseModel
from typing import Optional


class OrderItemInput(BaseModel):
    product_id: str
    quantity: int
    unit_price: int


class PlaceOrderRequest(BaseModel):
    items: list[OrderItemInput]
    delivery_address: str
    delivery_type: str = "standard"
    notes: str | None = None


class OrderStatusUpdate(BaseModel):
    status: str
    note: str | None = None


class AssignDriverRequest(BaseModel):
    driver_id: str


class LocationUpdateRequest(BaseModel):
    latitude: float
    longitude: float
    speed: Optional[float] = None
