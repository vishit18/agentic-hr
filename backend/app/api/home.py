"""Landing redirect by role."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse, Response

from app.core.auth import get_current_user, home_path
from app.core.models import User

router = APIRouter()


@router.get("/home")
def home(user: User = Depends(get_current_user)) -> Response:
    return RedirectResponse(home_path(user), status_code=303)
