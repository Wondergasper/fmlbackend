from pydantic import BaseModel


class ProductCreate(BaseModel):
    name: str
    category: str
    price: int
    stock: int
    origin: str
    description: str | None = None
    image_url: str | None = None


class ProductUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    price: int | None = None
    stock: int | None = None
    origin: str | None = None
    description: str | None = None
    image_url: str | None = None


class ProductStatusUpdate(BaseModel):
    status: str
    reason: str | None = None


class ProductResponse(BaseModel):
    id: str
    name: str
    category: str
    price: int
    stock: int
    origin: str
    description: str | None = None
    image_url: str | None = None
    status: str
    vendor_id: str
    created_at: str | None = None
    updated_at: str | None = None
