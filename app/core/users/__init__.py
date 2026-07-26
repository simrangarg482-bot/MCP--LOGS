"""core/users -- identity, RBAC, and permission resolution.

Owns the `users`, `roles`, `permissions`, `role_permissions`, and `user_roles`
tables (DATABASE_DESIGN.md: "core/ -- owned tables"). This submodule is the
authority on *who a user is* and *what they may do*: it turns a user row plus
their assigned roles into the flat permission set carried on an `Identity`,
and provides the `authorize()` check every other module relies on
(API_DESIGN.md section 2; ARCHITECTURE.md section 6).

Separation of concerns within RBAC:
  - This module RESOLVES identity + permissions from persisted role
    assignments and exposes `authorize()`.
  - core/auth turns a raw credential/token into a call to this module's
    resolver; it does not itself read roles/permissions.
That split keeps "how you prove who you are" (auth) separate from "who you are
and what you can do" (users/RBAC), so either can change without the other.

Callers use `from app.core.users.service import authorize` (etc.); this
package intentionally exposes nothing at import time.
"""