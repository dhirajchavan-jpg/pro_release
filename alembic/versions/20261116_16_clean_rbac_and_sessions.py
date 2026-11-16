"""clean rbac and multi-user sessions

Revision ID: 20261116_16
Revises: 20260923_15
Create Date: 2026-11-16
"""

from __future__ import annotations

import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261116_16"
down_revision = "20260923_15"
branch_labels = None
depends_on = None

JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


PERMISSIONS_DATA = [
    # Tenders
    ("tenders:create", "tenders", "Create new tender projects"),
    ("tenders:edit", "tenders", "Edit RFP documents and specifications"),
    ("tenders:freeze", "tenders", "Freeze tender criteria and RFP documents"),
    ("tenders:view", "tenders", "View tender projects and specifications"),
    # Bids
    ("bids:intake", "bids", "Upload and register bidder proposals"),
    ("bids:classify", "bids", "Classify uploaded bid packages"),
    ("bids:evaluate", "bids", "Score technical and operational criteria"),
    ("bids:view", "bids", "View bidder documents and evaluation results"),
    # Commercial
    ("commercial:quarantine", "commercial", "Quarantine commercial bid envelopes"),
    ("commercial:unseal", "commercial", "Unseal quarantined financial proposals"),
    ("commercial:view", "commercial", "View unsealed financial prices"),
    ("commercial:evaluate", "commercial", "Evaluate commercial rankings and L1 determination"),
    # Awards & Signoff
    ("awards:propose", "awards", "Propose tender award recommendations"),
    ("awards:approve", "awards", "Approve final contract awards"),
    ("awards:reject", "awards", "Reject awards and return for re-evaluation"),
    # Audit & Administration
    ("audit:read", "audit", "View tamper-proof audit trail and history"),
    ("users:manage", "admin", "Invite, update, and deactivate workspace users"),
    ("roles:manage", "admin", "Manage roles and permission mappings"),
    ("keys:manage", "admin", "Generate and revoke programmatic API keys"),
]

ROLE_PERMISSIONS_MAPPING = {
    "admin": [slug for slug, _, _ in PERMISSIONS_DATA],
    "procurement_officer": [
        "tenders:create", "tenders:edit", "tenders:freeze", "tenders:view",
        "bids:intake", "bids:classify", "bids:view",
        "commercial:view", "awards:propose", "audit:read",
    ],
    "reviewer": [
        "tenders:view", "bids:evaluate", "bids:view", "audit:read",
    ],
    "commercial_officer": [
        "tenders:view", "bids:view", "commercial:quarantine", "commercial:unseal",
        "commercial:view", "commercial:evaluate", "awards:propose", "audit:read",
    ],
    "approver": [
        "tenders:view", "bids:view", "commercial:view", "awards:approve", "awards:reject", "audit:read",
    ],
    "auditor": [
        "tenders:view", "bids:view", "commercial:view", "audit:read",
    ],
}


def upgrade() -> None:
    # 1. Drop legacy single-session table
    op.drop_table("local_sessions")

    # 2. Roles table
    op.create_table(
        "roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("name", sa.String(64), nullable=False, index=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 3. Permissions table
    op.create_table(
        "permissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("category", sa.String(64), nullable=False, index=True),
        sa.Column("description", sa.Text(), nullable=True),
    )

    # 4. Role-Permission junction table
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_id", sa.String(36), sa.ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
    )

    # 5. Add new columns to users
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.add_column("users", sa.Column("role_id", sa.String(36), sa.ForeignKey("roles.id", ondelete="SET NULL"), nullable=True, index=True))

    # 6. User sessions table (multi-session, keyed by token hash)
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 7. Scoped API keys table
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("scopes", JSON_DOC, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Seed permissions and system roles
    bind = op.get_bind()

    perm_id_map: dict[str, str] = {}
    for slug, category, desc in PERMISSIONS_DATA:
        p_id = str(uuid.uuid4())
        perm_id_map[slug] = p_id
        bind.execute(
            sa.text("INSERT INTO permissions (id, slug, category, description) VALUES (:id, :slug, :cat, :desc)"),
            {"id": p_id, "slug": slug, "cat": category, "desc": desc},
        )

    role_id_map: dict[str, str] = {}
    for role_name, slugs in ROLE_PERMISSIONS_MAPPING.items():
        r_id = str(uuid.uuid4())
        role_id_map[role_name] = r_id
        bind.execute(
            sa.text("INSERT INTO roles (id, tenant_id, name, description, is_system) VALUES (:id, NULL, :name, :desc, true)"),
            {"id": r_id, "name": role_name, "desc": f"System role: {role_name}"},
        )
        for slug in slugs:
            p_id = perm_id_map.get(slug)
            if p_id:
                bind.execute(
                    sa.text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:r_id, :p_id)"),
                    {"r_id": r_id, "p_id": p_id},
                )

    # Update existing users to link to their role_id
    for role_name, r_id in role_id_map.items():
        bind.execute(
            sa.text("UPDATE users SET role_id = :r_id WHERE role = :role_name AND role_id IS NULL"),
            {"r_id": r_id, "role_name": role_name},
        )


def downgrade() -> None:
    op.drop_table("api_keys")
    op.drop_table("user_sessions")
    op.drop_column("users", "role_id")
    op.drop_column("users", "is_active")
    op.drop_column("users", "password_hash")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")

    # Recreate legacy local_sessions
    op.create_table(
        "local_sessions",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_local_sessions_expires_at", "local_sessions", ["expires_at"])
