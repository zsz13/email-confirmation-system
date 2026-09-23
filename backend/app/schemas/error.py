from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """Shape of every error body the API returns (matches FastAPI's ``HTTPException``)."""

    detail: str
