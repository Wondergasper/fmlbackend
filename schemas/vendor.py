from pydantic import BaseModel


class VendorProfileUpdate(BaseModel):
    display_name: str | None = None
    bio: str | None = None
    farm_name: str | None = None
    location: str | None = None
    fulfillment_hub: str | None = None
    order_cutoff: str | None = None
    bank_name: str | None = None
    account_number: str | None = None
    account_name: str | None = None
    phone: str | None = None


class VendorStatusUpdate(BaseModel):
    status: str
    reason: str | None = None


class VendorResponse(BaseModel):
    id: str
    email: str
    full_name: str
    display_name: str | None = None
    bio: str | None = None
    farm_name: str | None = None
    location: str | None = None
    rating: float | None = None
    status: str
    products_count: int | None = None
