"""SCIM 2.0 provisioning / deprovisioning for ONE organization at a time.

An identity provider (Microsoft Entra ID, Okta) pushes user lifecycle changes to EKIP over SCIM; EKIP turns a
deactivation or deletion into the Phase-1 membership deactivation (`core.users.membership`) -- for that
organization only, never the global `users.is_active`.

Trust model: a SCIM bearer token belongs to exactly one organization and can do nothing outside it. A SCIM
identity is NOT an EKIP account: SCIM never creates users, never grants access and never links an identity to
an existing account by email. See `docs/operations/scim-provisioning.md`.
"""
