"""Reports, exports and Settings (users and retention)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request as HttpRequest
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.core.audit import audit
from app.core.auth import hash_password, require_role, require_staff
from app.core.db import get_db
from app.core.models import User
from app.core.web import render
from app.workflows import retention
from app.workflows.export_all import build_export
from app.workflows.reporting import current_quarter, quarterly_report, quarterly_workbook

router = APIRouter()
admin_only = require_role("hr_admin")


# ---------------------------------------------------------------- reports

def _period(year: int | None, quarter: int | None) -> tuple[int, int]:
    cy, cq = current_quarter()
    y = year if year and 2000 <= year <= 2100 else cy
    q = quarter if quarter in (1, 2, 3, 4) else cq
    return y, q


@router.get("/reports")
def reports(request: HttpRequest, year: int | None = None, quarter: int | None = None,
            user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    y, q = _period(year, quarter)
    cy, _ = current_quarter()
    return render(request, "reports.html", {"r": quarterly_report(db, y, q), "years": list(range(cy, cy - 5, -1))},
                  user=user, db=db, section="reports")


@router.get("/reports/export.xlsx")
def export_report(year: int | None = None, quarter: int | None = None, user: User = Depends(require_staff),
                  db: Session = Depends(get_db)) -> Response:
    y, q = _period(year, quarter)
    report = quarterly_report(db, y, q)
    data = quarterly_workbook(db, report)
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="export.report",
          output={"year": y, "quarter": q, "requests": report["received"]})
    db.commit()
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="cadence-requests-{y}-Q{q}.xlsx"'})


@router.get("/exports/all")
def export_all(user: User = Depends(admin_only), db: Session = Depends(get_db)) -> Response:
    tmp = Path(tempfile.mkstemp(prefix="cadence-export-", suffix=".zip")[1])
    counts = build_export(db, tmp, user.email)
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="export.all", output=counts)
    db.commit()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return FileResponse(tmp, media_type="application/zip", filename=f"cadence-export-{stamp}.zip",
                        background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)))


# ---------------------------------------------------------------- settings

ROLES = ("recruiter", "hr_admin", "engineer")


def _settings_ctx(db: Session, request: HttpRequest) -> dict:
    days = retention.get_days(db)
    return {"users": list(db.scalars(select(User).order_by(User.id))), "roles": ROLES, "days": days,
            "due_now": len(retention.due_candidates(db, days)) if days else None,
            "preview_days": request.query_params.get("preview"), "saved": request.query_params.get("saved"),
            "error": (request.query_params.get("error") or "").replace("+", " ")}


@router.get("/settings")
def settings_page(request: HttpRequest, user: User = Depends(admin_only), db: Session = Depends(get_db)) -> Response:
    ctx = _settings_ctx(db, request)
    pd = request.query_params.get("preview")
    if pd and pd.isdigit() and int(pd) > 0:
        ctx["preview_count"] = len(retention.due_candidates(db, int(pd)))
    return render(request, "settings.html", ctx, user=user, db=db, section="settings")


@router.post("/settings/retention")
def set_retention(days: int = Form(0), confirm: str = Form(""), user: User = Depends(admin_only),
                  db: Session = Depends(get_db)) -> Response:
    if days < 0 or days > 36500:
        return RedirectResponse("/settings?error=Enter+a+number+of+days+between+0+and+36500", status_code=303)
    if days > 0 and confirm != "1":
        return RedirectResponse(f"/settings?preview={days}", status_code=303)  # show how many would be removed first
    old = retention.get_days(db)
    retention.set_days(db, days, user.id)
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="retention.changed",
          output={"from_days": old, "to_days": days})
    db.commit()
    return RedirectResponse("/settings?saved=retention", status_code=303)


@router.post("/settings/users")
def create_user(email: str = Form(""), name: str = Form(""), role: str = Form("recruiter"), password: str = Form(""),
                user: User = Depends(admin_only), db: Session = Depends(get_db)) -> Response:
    email = email.strip().lower()[:320]
    if "@" not in email or not name.strip() or role not in ROLES or len(password) < 10:
        return RedirectResponse("/settings?error=Enter+a+valid+email,+a+name,+a+role+and+a+password+of+at+least+10+characters", status_code=303)
    if db.scalar(select(User.id).where(User.email == email)):
        return RedirectResponse("/settings?error=That+email+already+has+an+account", status_code=303)
    new = User(email=email, name=name.strip()[:200], role=role, password_hash=hash_password(password))
    db.add(new)
    db.flush()
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="user.created",
          entity_type="user", entity_id=new.id, output={"role": role})
    db.commit()
    return RedirectResponse("/settings?saved=user", status_code=303)


@router.post("/settings/users/{user_id:int}")
def update_user(user_id: int, role: str = Form(""), active: str = Form(""), admin: User = Depends(admin_only),
                db: Session = Depends(get_db)) -> Response:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    if role not in ROLES:
        raise HTTPException(status_code=400)
    is_active = active == "1"
    if target.id == admin.id and (role != "hr_admin" or not is_active):
        return RedirectResponse("/settings?error=You+cannot+remove+your+own+administrator+access", status_code=303)
    target.role, target.is_active = role, is_active
    audit(db, actor_type="user", actor_id=admin.id, actor_name=admin.email, action="user.updated",
          entity_type="user", entity_id=target.id, output={"role": role, "active": is_active})
    db.commit()
    return RedirectResponse("/settings?saved=user", status_code=303)
