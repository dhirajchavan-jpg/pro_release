"""Deterministic unit tests for modern SaaS authentication & authorization."""

import unittest
from unittest.mock import MagicMock
from fastapi import HTTPException, Request

from procurement.platform.auth import (
    Principal,
    create_access_token,
    decode_jwt,
    hash_local_password,
    verify_local_password,
    require_role,
    require_permission,
)


class TestCleanAuth(unittest.TestCase):
    def test_password_hashing_and_verification(self):
        password = "SuperSecretPassword123!"
        encoded = hash_local_password(password)

        self.assertTrue(encoded.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_local_password(password, encoded))
        self.assertFalse(verify_local_password("WrongPassword!", encoded))
        self.assertFalse(verify_local_password(password, "invalid_hash_string"))

    def test_jwt_access_token_uses_expected_claims(self):
        import os
        import jwt
        old_values = {key: os.environ.get(key) for key in ("JWT_SIGNING_KEY", "JWT_ISSUER", "JWT_AUDIENCE")}
        os.environ["JWT_SIGNING_KEY"] = "test_jwt_signing_key_1234567890abcdef"
        os.environ["JWT_ISSUER"] = "test-issuer"
        os.environ["JWT_AUDIENCE"] = "test-audience"
        try:
            token = create_access_token("user-1", "tenant-1", "user:test", "reviewer", 0, ttl_seconds=300)
            claims = jwt.decode(
                token,
                os.environ["JWT_SIGNING_KEY"],
                algorithms=["HS256"],
                audience="test-audience",
                issuer="test-issuer",
            )
            self.assertEqual(claims["sub"], "user:test")
            self.assertEqual(claims["user_id"], "user-1")
            self.assertEqual(claims["tenant_id"], "tenant-1")
            self.assertEqual(claims["roles"], ["reviewer"])
            self.assertEqual(claims["token_type"], "access")
            self.assertEqual(claims["token_version"], 0)
            with self.assertRaises(HTTPException):
                decode_jwt(token[:-1] + ("a" if token[-1] != "a" else "b"), "access")
            with self.assertRaises(HTTPException):
                decode_jwt(token, "refresh")
        finally:
            for key, value in old_values.items():
                if value is not None:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)

    def test_require_role(self):
        req = MagicMock(spec=Request)
        req.state.principal = Principal(
            subject="user:test",
            tenant_id="tenant-1",
            roles=frozenset({"reviewer"}),
            permissions=frozenset({"bids:evaluate"}),
        )

        # Allowed role
        p = require_role(req, "reviewer", "admin")
        self.assertEqual(p.subject, "user:test")

        # Forbidden role
        with self.assertRaises(HTTPException) as ctx:
            require_role(req, "procurement_officer", "approver")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_bypasses_role_and_permission_checks(self):
        req = MagicMock(spec=Request)
        req.state.principal = Principal(
            subject="admin:super",
            tenant_id="tenant-1",
            roles=frozenset({"admin"}),
            permissions=frozenset(),
        )

        # Admin passes any require_role check
        p = require_role(req, "auditor")
        self.assertIn("admin", p.roles)

        # Admin passes any require_permission check even if permissions set is empty
        p = require_permission(req, "commercial:unseal")
        self.assertIn("admin", p.roles)

    def test_require_permission(self):
        req = MagicMock(spec=Request)
        req.state.principal = Principal(
            subject="evaluator:jane",
            tenant_id="tenant-1",
            roles=frozenset({"evaluator"}),
            permissions=frozenset({"bids:evaluate", "bids:view"}),
        )

        # Allowed permission
        p = require_permission(req, "bids:evaluate")
        self.assertEqual(p.subject, "evaluator:jane")

        # Insufficient permission
        with self.assertRaises(HTTPException) as ctx:
            require_permission(req, "commercial:unseal")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "insufficient permissions")

    def test_workspace_invite_model_and_token_hash(self):
        import hashlib
        import secrets
        from datetime import datetime, timedelta, timezone
        from procurement.domain.models import WorkspaceInvite

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires = datetime.now(timezone.utc) + timedelta(days=7)

        invite = WorkspaceInvite(
            tenant_id="tenant-1",
            email="invited.officer@domain.gov",
            role="procurement_officer",
            token_hash=token_hash,
            expires_at=expires,
        )

        self.assertEqual(invite.role, "procurement_officer")
        self.assertEqual(invite.email, "invited.officer@domain.gov")
        self.assertEqual(invite.token_hash, token_hash)
        self.assertIsNone(invite.accepted_at)


if __name__ == "__main__":
    unittest.main()
