"""Admin API (role=admin only; enforced by the auth middleware in server/app.py).

  GET    /api/v1/admin/users                 list accounts
  POST   /api/v1/admin/users                 {username, password, role}
  PATCH  /api/v1/admin/users/{id}            {role?, active?, password?}
  DELETE /api/v1/admin/users/{id}
  GET    /api/v1/admin/sources               per-source counts + last scrape
  GET    /api/v1/admin/runs?limit=50         collector runs
  GET    /api/v1/admin/overrides             manual camera corrections
  PUT    /api/v1/admin/overrides/{cam_id}    {hidden?, name?, lat?, lon?, note?}
  DELETE /api/v1/admin/overrides/{cam_id}
  GET    /api/v1/admin/audit?limit&offset&action&username
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from collector.common import valid_ll
from db.store import get_store

from . import auth

router = APIRouter()


def _who(request: Request):
    u = getattr(request.state, "user", None) or {}
    return u.get("username"), client_ip(request)


def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def _err(e: Exception):
    raise HTTPException(400, str(e))


# ------------------------------------------------------------------ users
class NewUser(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UserPatch(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


@router.get("/users")
def users():
    return auth.list_users()


@router.post("/users", status_code=201)
def create_user(body: NewUser, request: Request):
    who, ip = _who(request)
    try:
        uid = auth.create_user(body.username, body.password, body.role)
    except auth.AuthError as e:
        _err(e)
    auth.audit(who, "user_create", body.username.lower(), {"role": body.role}, ip)
    return {"id": uid}


@router.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, request: Request):
    who, ip = _who(request)
    try:
        u = auth.update_user(user_id, body.role, body.active, body.password)
    except auth.AuthError as e:
        _err(e)
    detail = {k: v for k, v in body.model_dump().items() if v is not None and k != "password"}
    if body.password is not None:
        detail["password"] = "reset"
    auth.audit(who, "user_update", u["username"], detail, ip)
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request):
    who, ip = _who(request)
    me = getattr(request.state, "user", {}) or {}
    if me.get("id") == user_id:
        raise HTTPException(400, "you can't delete your own account")
    try:
        u = auth.delete_user(user_id)
    except auth.AuthError as e:
        _err(e)
    auth.audit(who, "user_delete", u["username"], {}, ip)
    return {"ok": True}


# ------------------------------------------------------------------ collector & health
@router.get("/sources")
def sources():
    store = get_store()
    with store.pool.connection() as c:
        counts = c.execute(
            """SELECT c.source,
                      count(*) FILTER (WHERE c.active) AS active,
                      count(*) FILTER (WHERE c.active AND c.status = 'online') AS online,
                      count(*) FILTER (WHERE c.active AND c.status = 'offline') AS offline,
                      count(*) FILTER (WHERE NOT c.active) AS retired,
                      count(*) FILTER (WHERE COALESCE(o.hidden, false)) AS hidden,
                      max(c.last_checked) AS last_checked
               FROM cameras c LEFT JOIN camera_overrides o ON o.camera_id = c.id
               GROUP BY c.source""").fetchall()
        last = c.execute(
            """SELECT DISTINCT ON (source) source, started_at, finished_at, mode, cameras, added, removed, error
               FROM collector_runs ORDER BY source, id DESC""").fetchall()
        last_live = c.execute(
            "SELECT source, max(finished_at) AS at FROM collector_runs WHERE mode = 'live' GROUP BY source").fetchall()
        names = c.execute("SELECT id, name, homepage FROM sources").fetchall()
    by = {r["id"]: {"id": r["id"], "name": r["name"], "homepage": r["homepage"]} for r in names}
    for r in counts:
        by.setdefault(r["source"], {"id": r["source"]}).update(store._iso(r))
    for r in last:
        by.setdefault(r["source"], {"id": r["source"]})["last_run"] = store._iso(r)
    for r in last_live:
        by.setdefault(r["source"], {"id": r["source"]})["last_live_success"] = r["at"].isoformat() if r["at"] else None
    return sorted(by.values(), key=lambda x: x["id"])


@router.get("/runs")
def runs(limit: int = Query(50, ge=1, le=500)):
    return get_store().recent_runs(limit)


# ------------------------------------------------------------------ camera overrides
class Override(BaseModel):
    hidden: bool = False
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    note: Optional[str] = None


@router.get("/overrides")
def overrides():
    store = get_store()
    with store.pool.connection() as c:
        rows = c.execute(
            """SELECT o.*, c.name AS source_name, c.source, c.lat AS source_lat, c.lon AS source_lon, c.active
               FROM camera_overrides o JOIN cameras c ON c.id = o.camera_id ORDER BY o.updated_at DESC""").fetchall()
    return [store._iso(r) for r in rows]


@router.put("/overrides/{cam_id:path}")
def set_override(cam_id: str, body: Override, request: Request):
    store = get_store()
    if not store.get_camera(cam_id):
        raise HTTPException(404, "unknown camera")
    name = (body.name or "").strip() or None
    if (body.lat is None) != (body.lon is None):
        raise HTTPException(400, "give both lat and lon, or neither")
    if body.lat is not None and not valid_ll(body.lat, body.lon):
        raise HTTPException(400, "location must be inside Indonesia (lat −11.5…6.5, lon 94…141.5)")
    if not body.hidden and name is None and body.lat is None and not (body.note or "").strip():
        raise HTTPException(400, "nothing to override — use Reset instead")
    who, ip = _who(request)
    with store.pool.connection() as c:
        c.execute(
            """INSERT INTO camera_overrides (camera_id, hidden, name, lat, lon, note, updated_by, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (camera_id) DO UPDATE SET hidden = EXCLUDED.hidden, name = EXCLUDED.name,
                 lat = EXCLUDED.lat, lon = EXCLUDED.lon, note = EXCLUDED.note,
                 updated_by = EXCLUDED.updated_by, updated_at = now()""",
            (cam_id, body.hidden, name, body.lat, body.lon, (body.note or "").strip() or None, who))
    auth.audit(who, "override_set", cam_id, body.model_dump(), ip)
    return store.get_camera(cam_id)


@router.delete("/overrides/{cam_id:path}")
def delete_override(cam_id: str, request: Request):
    store = get_store()
    with store.pool.connection() as c:
        n = c.execute("DELETE FROM camera_overrides WHERE camera_id = %s", (cam_id,)).rowcount
    if not n:
        raise HTTPException(404, "no override for this camera")
    who, ip = _who(request)
    auth.audit(who, "override_reset", cam_id, {}, ip)
    return {"ok": True}


# ------------------------------------------------------------------ audit
@router.get("/audit")
def audit(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
          action: Optional[str] = None, username: Optional[str] = None):
    return auth.list_audit(limit, offset, action, username)
