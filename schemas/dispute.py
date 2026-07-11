from pydantic import BaseModel


class CreateDisputeRequest(BaseModel):
    order_id: str
    reason: str


class AddNoteRequest(BaseModel):
    note_text: str


class AddEvidenceRequest(BaseModel):
    file_url: str | None = None
    file_path: str | None = None
    file_type: str | None = None
    description: str | None = None


class ResolveDisputeRequest(BaseModel):
    resolution_outcome: str
    resolution_notes: str | None = None
