from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.config import settings
from app.deps import get_session, require_admin
from app.database import fetch_all, fetch_one, execute
from app.services.ldap_auth import test_ldap_connection
from app.utils.password import hash_password

router = APIRouter()


@router.get("")
async def list_users(session: dict = Depends(require_admin)):
    rows = await fetch_all(
        "SELECT id, username, display_name, email, role, ldap_dn, last_login, created_at "
        "FROM users ORDER BY id"
    )
    for r in rows:
        r["is_ldap"] = bool(r.get("ldap_dn"))
        r.pop("ldap_dn", None)
    return rows


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    display_name: str = ""
    email: str = ""


@router.post("")
async def create_user(body: CreateUserBody, session: dict = Depends(require_admin)):
    if not body.username:
        raise HTTPException(400, "Username required")
    if len(body.password) < settings.password_min_length:
        raise HTTPException(400, f"Password min {settings.password_min_length} chars")
    if body.role not in ("admin", "operator", "viewer"):
        raise HTTPException(400, "Invalid role")
    try:
        uid = await execute(
            "INSERT INTO users (username, password_hash, role, display_name, email) VALUES (%s,%s,%s,%s,%s)",
            (body.username, hash_password(body.password), body.role,
             body.display_name or None, body.email or None),
        )
        return {"ok": True, "id": uid}
    except Exception as e:
        if "Duplicate" in str(e):
            raise HTTPException(409, "Username already exists")
        raise


class UpdateUserBody(BaseModel):
    id: int
    role: Optional[str] = None
    display_name: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None


@router.put("/{user_id}")
async def update_user(user_id: int, body: UpdateUserBody, session: dict = Depends(require_admin)):
    sets, vals = [], []
    if body.role is not None:
        if body.role not in ("admin", "operator", "viewer"):
            raise HTTPException(400, "Invalid role")
        if user_id == int(session["uid"]) and body.role != "admin":
            raise HTTPException(400, "Cannot change your own role")
        sets.append("role=%s");         vals.append(body.role)
    if body.display_name is not None:
        sets.append("display_name=%s"); vals.append(body.display_name or None)
    if body.email is not None:
        sets.append("email=%s");        vals.append(body.email or None)
    if body.password:
        if len(body.password) < settings.password_min_length:
            raise HTTPException(400, f"Password min {settings.password_min_length} chars")
        sets.append("password_hash=%s"); vals.append(hash_password(body.password))
    if not sets:
        raise HTTPException(400, "Nothing to update")
    vals.append(user_id)
    await execute("UPDATE users SET " + ",".join(sets) + " WHERE id=%s", tuple(vals))
    return {"ok": True}


@router.delete("/{user_id}")
async def delete_user(user_id: int, session: dict = Depends(require_admin)):
    if user_id == int(session["uid"]):
        raise HTTPException(400, "Cannot delete yourself")
    await execute("DELETE FROM users WHERE id=%s", (user_id,))
    return {"ok": True}


@router.get("/ldap")
async def get_ldap_config(session: dict = Depends(require_admin)):
    row = await fetch_one("SELECT * FROM ldap_config WHERE id=1")
    if row:
        row["bind_pass_masked"] = "*" * 16 if row.get("bind_pass") else ""
        row["bind_pass"] = ""
    return row or {}


class LdapConfigBody(BaseModel):
    host: str = ""
    port: int = 389
    base_dn: str = ""
    bind_dn: str = ""
    bind_pass: str = ""
    user_filter: str = "(&(objectClass=user)(sAMAccountName=%s))"
    admin_group: str = ""
    operator_group: str = ""
    use_tls: bool = False
    enabled: bool = False


@router.put("/ldap")
async def save_ldap_config(body: LdapConfigBody, session: dict = Depends(require_admin)):
    sets = [
        "host=%s", "port=%s", "base_dn=%s", "bind_dn=%s",
        "user_filter=%s", "admin_group=%s", "operator_group=%s",
        "use_tls=%s", "enabled=%s",
    ]
    vals = [
        body.host, body.port, body.base_dn, body.bind_dn,
        body.user_filter, body.admin_group, body.operator_group,
        int(body.use_tls), int(body.enabled),
    ]
    # Only update bind_pass if it's not masked
    if body.bind_pass and "*" not in body.bind_pass:
        sets.append("bind_pass=%s")
        vals.append(body.bind_pass)
    vals.append(1)
    await execute("UPDATE ldap_config SET " + ",".join(sets) + " WHERE id=%s", tuple(vals))
    return {"ok": True}


class LdapTestBody(BaseModel):
    host: str
    port: int = 389
    base_dn: str
    bind_dn: str
    bind_pass: str
    use_tls: bool = False


@router.post("/ldap/test")
async def test_ldap(body: LdapTestBody, session: dict = Depends(require_admin)):
    bind_pass = body.bind_pass
    if "*" in bind_pass:
        row = await fetch_one("SELECT bind_pass FROM ldap_config WHERE id=1")
        bind_pass = row["bind_pass"] if row else ""
    result = await test_ldap_connection(
        body.host, body.port, body.bind_dn, bind_pass, body.base_dn, body.use_tls
    )
    if not result["ok"]:
        raise HTTPException(400, result.get("error", "LDAP test failed"))
    return result
