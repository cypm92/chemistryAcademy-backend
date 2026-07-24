from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class AdminUserCreate(UserCreate):
    role: str = "guest"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    name: str
    email: EmailStr
    role: str
    is_active: bool
    model_config = ConfigDict(from_attributes=True)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class GrantCreate(BaseModel):
    user_id: int
    material_id: int
    starts_at: datetime | None = None
    expires_at: datetime
    can_download: bool = False


class GrantOut(BaseModel):
    id: int
    user_id: int
    material_id: int
    starts_at: datetime
    expires_at: datetime
    can_download: bool
    model_config = ConfigDict(from_attributes=True)


class GrantUpdate(BaseModel):
    expires_at: datetime
    can_download: bool | None = None


class GrantWithUserOut(GrantOut):
    user: UserOut


class MaterialOut(BaseModel):
    id: int
    title: str
    description: str
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    expires_at: datetime | None = None
    can_download: bool = False
    model_config = ConfigDict(from_attributes=True)


class AdminMaterialOut(MaterialOut):
    grants: list[GrantWithUserOut] = []
