from pydantic import BaseModel


class TextInputRequest(BaseModel):
    text: str


class VisitorRecordResponse(BaseModel):
    id: int
    session_id: str
    plate_number: str | None = None
    target: str | None = None
    reason: str | None = None
    phone: str | None = None
    entry_time: str | None = None
    status: str
    transcript: str | None = None
    missing_fields: str | None = None

    class Config:
        from_attributes = True