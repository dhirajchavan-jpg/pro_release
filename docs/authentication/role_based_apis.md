Role-based APIs are now implemented and documented in Swagger.

New bearer-protected endpoints:
- GET /workbench/account/permissions — list available permission slugs
- GET /workbench/account/roles — list system and workspace roles
- POST /workbench/account/roles — create a custom workspace role
- PATCH /workbench/account/roles/{role_id} — update a custom role
- DELETE /workbench/account/roles/{role_id} — delete an unused custom role

Rules enforced:
- The six built-in roles remain read-only.
- Custom roles are tenant-scoped.
- A role name cannot duplicate a built-in or workspace role.
- Unknown permissions are rejected.
- A custom role cannot be deleted while users or pending invitations use it.
- The final active admin cannot be demoted or deactivated.
- User, invite, and API-key endpoints now respect permission slugs (users:manage, keys:manage, roles:manage) instead of only checking for the admin role.
The new DB migration was applied successfully: 20261117_18.