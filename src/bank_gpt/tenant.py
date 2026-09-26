"""Fictional tenant fixture and the single data-access authorization boundary."""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv("BANKGPT_DEMO_DB", str(ROOT / "data" / "mock_bank.sqlite3")))
ISSUER = "bank-gpt-demo"
ROLE_PERMISSIONS = {
    "reader": {"balance:read", "capability:run"},
    "operator": {"balance:read", "capability:run", "run:operate"},
}


@dataclass(frozen=True)
class Actor:
    id: int
    username: str


def password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)


class TenantGateway:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.seed()
        self.path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def seed(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tenants (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    salt BLOB NOT NULL, password_digest BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_tenant_roles (
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                    role TEXT NOT NULL CHECK(role IN ('reader', 'operator')),
                    PRIMARY KEY (user_id, tenant_id)
                );
                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                    member_id TEXT NOT NULL, name TEXT NOT NULL,
                    UNIQUE (tenant_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                    kind TEXT NOT NULL CHECK(kind IN ('savings', 'checking')),
                    balance_cents INTEGER NOT NULL,
                    UNIQUE (customer_id, kind)
                );
            """)
            if db.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
                if not db.execute("SELECT 1 FROM users WHERE username='pine_operator'").fetchone():
                    salt = os.urandom(16)
                    db.execute("INSERT INTO users VALUES (?,?,?,?)",
                               (6, "pine_operator", salt,
                                password_hash("pine-operator-pass", salt)))
                    db.execute("INSERT INTO user_tenant_roles VALUES (?,?,?)",
                               (6, 2, "operator"))
                return
            db.executemany("INSERT INTO tenants(id,name) VALUES (?,?)", [
                (1, "North Harbor Credit Union"), (2, "Pine Valley Bank"),
                (3, "Cedar Community Bank")])
            credentials = [
                (1, "north_reader", "north-demo-pass", [(1, "reader")]),
                (2, "pine_reader", "pine-demo-pass", [(2, "reader")]),
                (3, "shared_reader", "shared-demo-pass", [(1, "reader"), (2, "reader")]),
                (4, "cedar_operator", "cedar-demo-pass", [(3, "operator")]),
                (5, "north_operator", "north-operator-pass", [(1, "operator")]),
                (6, "pine_operator", "pine-operator-pass", [(2, "operator")]),
            ]
            for user_id, username, password, memberships in credentials:
                salt = os.urandom(16)
                db.execute("INSERT INTO users VALUES (?,?,?,?)",
                           (user_id, username, salt, password_hash(password, salt)))
                db.executemany("INSERT INTO user_tenant_roles VALUES (?,?,?)",
                               [(user_id, tenant_id, role) for tenant_id, role in memberships])
            for tenant_id, count in [(1, 9), (2, 8), (3, 8)]:
                for index in range(1, count + 1):
                    member_id = str(tenant_id * 10000 + index)
                    cursor = db.execute(
                        "INSERT INTO customers(tenant_id,member_id,name) VALUES (?,?,?)",
                        (tenant_id, member_id, f"Example Member {tenant_id}-{index:02d}"))
                    db.execute("INSERT INTO accounts(customer_id,kind,balance_cents) VALUES (?,?,?)",
                               (cursor.lastrowid, "savings", 24138 + tenant_id * 10000 + index * 137))
                    db.execute("INSERT INTO accounts(customer_id,kind,balance_cents) VALUES (?,?,?)",
                               (cursor.lastrowid, "checking", 10000 + index * 211))

    def authenticate(self, username: str, password: str) -> Actor | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            password_hash(password, b"0" * 16)
            return None
        if not hmac.compare_digest(password_hash(password, row["salt"]), row["password_digest"]):
            return None
        return Actor(row["id"], row["username"])

    def actor(self, user_id: int) -> Actor:
        with self.connect() as db:
            row = db.execute("SELECT id,username FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise PermissionError("unknown actor")
        return Actor(row["id"], row["username"])

    def require(self, actor: Actor, tenant_id: int, permission: str) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT role FROM user_tenant_roles WHERE user_id=? AND tenant_id=?",
                (actor.id, tenant_id)).fetchone()
        if row is None or permission not in ROLE_PERMISSIONS[row["role"]]:
            raise PermissionError("tenant permission denied")

    def balance(self, actor: Actor, tenant_id: int, member_id: str) -> dict[str, Any] | None:
        self.require(actor, tenant_id, "balance:read")
        with self.connect() as db:
            row = db.execute("""
                SELECT c.member_id,c.name,a.balance_cents FROM customers c
                JOIN accounts a ON a.customer_id=c.id AND a.kind='savings'
                WHERE c.tenant_id=? AND c.member_id=?
            """, (tenant_id, member_id)).fetchone()
        return dict(row) if row else None

    def issue_permission(self, actor: Actor, tenant_id: int, capability_id: str,
                         operation: str = "replay") -> str:
        if operation not in {"replay", "evaluate"}:
            raise PermissionError("unsupported capability operation")
        self.require(actor, tenant_id, "capability:run")
        return issue_token(actor, audience="bank-gpt-capability", tenant_id=tenant_id,
                           capability_id=capability_id, operation=operation,
                           grant_id=str(uuid4()), ttl=120)


GATEWAY = TenantGateway()


def issue_token(actor: Actor, *, audience: str, tenant_id: int | None = None,
                run_id: str | None = None, capability_id: str | None = None,
                operation: str | None = None, grant_id: str | None = None,
                ttl: int = 900) -> str:
    signing_key = os.getenv("BANKGPT_SIGNING_KEY", "")
    if len(signing_key) < 32:
        raise RuntimeError("BANKGPT_SIGNING_KEY must be at least 32 characters")
    now = int(time.time())
    token_types = {"bank-gpt-api": "identity", "bank-gpt-browser": "browser_session",
                   "bank-gpt-capability": "secure_permissions"}
    if audience not in token_types:
        raise ValueError("unsupported token audience")
    claims: dict[str, Any] = {"iss": ISSUER, "aud": audience, "typ": token_types[audience],
                              "sub": str(actor.id),
                              "iat": now, "nbf": now, "exp": now + ttl}
    if tenant_id is not None:
        claims["tenant_id"] = tenant_id
    if run_id is not None:
        claims["run_id"] = run_id
    if capability_id is not None:
        claims["capability_id"] = capability_id
    if operation is not None:
        claims["operation"] = operation
    if grant_id is not None:
        claims["jti"] = grant_id
    return jwt.encode(claims, signing_key, algorithm="HS256")


def verify_token(token: str, audience: str) -> tuple[Actor, dict[str, Any]]:
    signing_key = os.getenv("BANKGPT_SIGNING_KEY", "")
    if len(signing_key) < 32:
        raise RuntimeError("BANKGPT_SIGNING_KEY must be at least 32 characters")
    try:
        claims = jwt.decode(token, signing_key, algorithms=["HS256"],
                            issuer=ISSUER, audience=audience,
                            options={"require": ["iss", "aud", "sub", "iat", "nbf", "exp"]})
        expected_type = {"bank-gpt-api": "identity", "bank-gpt-browser": "browser_session",
                         "bank-gpt-capability": "secure_permissions"}[audience]
        if claims.get("typ") != expected_type:
            raise ValueError("wrong token type")
        required = {"bank-gpt-api": set(), "bank-gpt-browser": {"tenant_id", "run_id"},
                    "bank-gpt-capability": {"tenant_id", "capability_id", "operation", "jti"}}[audience]
        forbidden = ({"tenant_id", "run_id", "capability_id", "operation", "jti"} - required)
        if not required <= claims.keys() or forbidden & claims.keys():
            raise ValueError("token claims do not match type")
        if audience == "bank-gpt-capability" and claims["operation"] not in {"replay", "evaluate"}:
            raise ValueError("unsupported grant operation")
        actor = GATEWAY.actor(int(claims["sub"]))
        return actor, claims
    except (jwt.PyJWTError, ValueError, PermissionError) as error:
        raise PermissionError("invalid or expired token") from error
