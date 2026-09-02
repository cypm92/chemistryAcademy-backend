from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class AdminUserCreate(UserCreate):
    role: str = "guest"


class AdminUserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: str | None = None
    is_active: bool | None = None


class ProfileUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    current_password: str | None = Field(default=None, min_length=8, max_length=128)
    new_password: str | None = Field(default=None, min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    name: str
    email: EmailStr
    role: str
    is_active: bool
    has_avatar: bool
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


class TagOut(BaseModel):
    id: int
    name: str
    model_config = ConfigDict(from_attributes=True)


class TagGrantCreate(BaseModel):
    user_id: int
    tag_id: int
    starts_at: datetime | None = None
    expires_at: datetime
    can_download: bool = False


class GrantWithUserOut(GrantOut):
    user: UserOut


class SharedMaterialOut(BaseModel):
    id: int
    title: str
    filename: str
    kind: str

    model_config = ConfigDict(from_attributes=True)


class GrantWithMaterialOut(GrantOut):
    material: SharedMaterialOut


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    parent_id: int | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class FolderUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    parent_id: int | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class FolderOut(BaseModel):
    id: int
    name: str
    parent_id: int | None
    path: str
    color: str | None = None
    effective_color: str | None = None


class ThemeOut(BaseModel):
    primary_color: str


class ThemeUpdate(BaseModel):
    primary_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class BrandingOut(ThemeOut):
    has_custom_logo: bool = False


class MaterialOut(BaseModel):
    id: int
    title: str
    description: str
    folder: FolderOut | None = None
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    expires_at: datetime | None = None
    can_download: bool = False
    tags: list[TagOut] = []
    is_favorite: bool = False
    model_config = ConfigDict(from_attributes=True)


class AdminMaterialOut(MaterialOut):
    grants: list[GrantWithUserOut] = []


class BookingCreate(BaseModel):
    starts_at: datetime
    subject: str = Field(min_length=3, max_length=100)
    duration_slots: int = Field(ge=1, le=4)


class BookingBlockCreate(BaseModel):
    starts_at: datetime
    ends_at: datetime
    status: str
    note: str = Field(default="", max_length=300)


class AdminBookingCreate(BaseModel):
    user_id: int
    starts_at: datetime
    subject: str = Field(min_length=3, max_length=100)
    duration_slots: int = Field(ge=1, le=4)
    admin_comment: str = Field(default="", max_length=1000)


class AdminClassUpdate(BaseModel):
    starts_at: datetime
    duration_slots: int = Field(ge=1, le=4)
    subject: str = Field(min_length=3, max_length=100)
    admin_comment: str = Field(default="", max_length=1000)


class BookingStatusUpdate(BaseModel):
    status: str


class ClassHistoricalUpdate(BaseModel):
    is_historical: bool


class BookingOut(BaseModel):
    id: int
    starts_at: datetime
    ends_at: datetime
    status: str
    note: str = ""
    admin_comment: str = ""
    user_id: int | None = None
    user_name: str | None = None
    is_mine: bool = False


class BookingRequestOut(BookingOut):
    user_email: EmailStr | None = None


class BookingMaterialCreate(BaseModel):
    material_id: int


class ClassOut(BaseModel):
    id: int
    starts_at: datetime
    ends_at: datetime
    status: str
    is_historical: bool = False
    topic: str
    admin_comment: str = ""
    user_id: int
    user_name: str
    user_email: EmailStr
    materials: list[SharedMaterialOut] = []
