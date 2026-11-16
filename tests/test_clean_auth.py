"""Deterministic unit tests for modern SaaS authentication & authorization."""

import unittest
from unittest.mock import MagicMock
from fastapi import HTTPException, Request

from procurement.platform.auth import (
    Principal,
    hash_local_password,
    verify_local_password,
    sign_session_token,
    verify_session_token,
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

    def test_session_token_signing_and_verification(self):
        import os
        old = os.environ.get("SESSION_SECRET_KEY")
        os.environ["SESSION_SECRET_KEY"] = "test_secret_key_1234567890abcdef"
        try:
            token = "test_opaque_token_string"
            signed = sign_session_token(token)

            self.assertTrue(signed.startswith(f"{token}."))

            # Valid token
            verified = verify_session_token(signed)
            self.assertEqual(verified, token)

            # Tampered token
            tampered = signed[:-4] + "xxxx"
            self.assertIsNone(verify_session_token(tampered))

            # Malformed format
            self.assertIsNone(verify_session_token("just_a_token_without_signature"))
        finally:
            if old is not None:
                os.environ["SESSION_SECRET_KEY"] = old
            else:
                os.environ.pop("SESSION_SECRET_KEY", None)

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
