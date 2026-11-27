"""Why it exists: To support secure refresh tokens that are stored as hashes, 
can be revoked, and support refresh-token rotation.
Cascade deletion: If a user or organization is deleted, 
its associated refresh-token records are automatically deleted 
because they are temporary session data, not permanent business records.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class RefreshToken(Base):
    """One issued refresh token 
    Rotation / reuse detection: every refresh token belongs to a `family_id`,
    shared by every token descended from one original login. When a token is
    used, core/auth's service issues a new token *in the same family* and
    marks this row `revoked_at` (rotation) rather than reusing the row. If a
    token whose `revoked_at` is already set is ever presented again, that is
    a reuse signal -- the entire `family_id` should be revoked immediately,
    since it indicates the token was very likely stolen and the legitimate
    client's copy has since been rotated away from underneath the attacker's
    copy. `family_id` (rather than walking a `replaced_by` linked list) makes
    "revoke this whole compromised chain" a single-column filter.

    Never stores the raw token value: `token_hash` is a cryptographic hash
    (computed by core/auth/service.py, not here) of the token actually handed
    to the client -- a stolen database row is useless without also finding a
    hash collision, matching the "never in plaintext" requirement.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_org", "user_id", "organization_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Shared by every token descended from one original login; not a foreign
    # key to anything -- it's a grouping id generated at first issuance, not
    # a reference to a row that itself exists.
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # SHA-256 (or equivalent) hash of the actual token value handed to the
    # client. Unique: two distinct tokens hashing to the same value would
    # otherwise be indistinguishable at lookup time.
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set on rotation (this token was exchanged for a new one) or explicit
    # revocation (logout, admin-forced termination, reuse-detected family
    # revocation). NULL means "still valid, if not expired."
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The MCP OAuth client this token was issued to (`oauth_clients.client_id`),
    # or NULL for an ordinary REST session. A bound token can only be refreshed
    # or revoked by that same client, and an unbound one can never be presented
    # by an OAuth client. Not a foreign key: OAuth clients are platform-wide
    # registry rows that may be pruned independently of sessions.
    oauth_client_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Deliberately no `updated_at`: the only mutation this row ever undergoes
    # is being revoked/rotated, and `revoked_at` itself already captures that
    # timestamp -- a separate generic `updated_at` would be redundant.


class EmailVerification(Base):
    """One outstanding proof-of-mailbox-ownership challenge.

    Three kinds, one lifecycle (issue -> deliver -> atomically consume):

    * `registration`: a self-service signup that has NOT yet created an
      organization, user, role or session. Carries the pending payload
      (display name, bcrypt hash, organization name/slug). Consuming the
      token is what creates all of those, in one transaction.
    * `invitation`: acceptance of an organization invitation with a password
      for an email that has no account yet. Carries the invitation id and the
      pending credential; the invitation stays `pending` until consumption.
    * `existing_user`: a legacy `users` row (`email_verified_at IS NULL`)
      proving ownership of its address.

    Deliberately NOT row-level-security protected: like `users` it is global
    and is read *before* any organization exists (there is no
    `organization_id` to scope by). It is reachable only through
    `core.auth.email_verification`; no tenant-scoped query reads it.

    `token_hash` is SHA-256 of a 256-bit random token that exists in cleartext
    only in memory between generation and the outgoing mail. `consumed_at`
    is set by a single atomic `UPDATE ... WHERE consumed_at IS NULL AND
    expires_at > now() RETURNING` -- that statement is the single-use gate.
    """

    __tablename__ = "email_verifications"
    __table_args__ = (
        Index(
            "uq_email_verifications_open_email_kind",
            "email",
            "kind",
            unique=True,
            postgresql_where=text("consumed_at IS NULL"),
        ),
        Index("ix_email_verifications_expires_at", "expires_at"),
        CheckConstraint(
            "kind IN ('registration', 'invitation', 'existing_user')",
            name="ck_email_verifications_kind",
        ),
        CheckConstraint(
            "(kind <> 'registration') OR (display_name IS NOT NULL AND password_hash IS NOT NULL "
            "AND organization_name IS NOT NULL AND organization_slug IS NOT NULL)",
            name="ck_email_verifications_registration_payload",
        ),
        CheckConstraint(
            "(kind <> 'invitation') OR (invitation_id IS NOT NULL AND display_name IS NOT NULL "
            "AND password_hash IS NOT NULL)",
            name="ck_email_verifications_invitation_payload",
        ),
        CheckConstraint(
            "(kind <> 'existing_user') OR (user_id IS NOT NULL)",
            name="ck_email_verifications_existing_user_payload",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: Normalised (stripped, lower-cased) address the token was mailed to.
    email: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    send_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    send_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- pending payload (kind-dependent; see CHECK constraints) ---
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: bcrypt hash, never the plaintext password.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    organization_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    organization_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    invitation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invitations.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PasswordReset(Base):
    """One outstanding "forgot password" link.

    Global like `users` and `email_verifications` (NOT row-level-security
    protected): a reset is requested and consumed before any session or
    organization exists, and a password is a USER-level credential that spans
    every organization the person belongs to. Reachable only through
    `core.auth.password_reset`.

    `token_hash` is SHA-256 of a 256-bit random token that exists in cleartext
    only in memory between generation and the outgoing mail. `consumed_at` is
    set by a single atomic `UPDATE ... WHERE consumed_at IS NULL AND expires_at
    > now() RETURNING` -- the single-use gate, in the same transaction as the
    password change and the session revocation. At most ONE open link per user
    (partial unique index): asking again rotates it, which kills the earlier
    link.
    """

    __tablename__ = "password_resets"
    __table_args__ = (
        Index(
            "uq_password_resets_open_user",
            "user_id",
            unique=True,
            postgresql_where=text("consumed_at IS NULL"),
        ),
        Index("ix_password_resets_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    send_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    send_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
