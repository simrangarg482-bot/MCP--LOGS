"""SCIM groups and role provenance: groups, memberships, explicit group-to-role mappings, role grants

Revision ID: c1a5e7f3b9d2
Revises: b9f4d6e2a1c8
Create Date: 2026-11-28 15:00:00.000000

Phase 4 of automated user offboarding. Additive.

* `user_roles.granted_via` (`manual` | `scim`, default `manual`): PROVENANCE of a role assignment. Every
  existing row becomes `manual` through the column default, so **synchronization can never remove a role
  that existed before this migration, or one an administrator assigned**. Only a role SCIM itself created
  is `scim`.
* `scim_groups` -- a group the identity provider pushed, with `display_name_key` (case-insensitive unique per
  organization) and an optional `external_id`. A group grants NOTHING until an administrator maps it.
* `scim_group_members` -- which SCIM identities the provider says belong to the group (pending, not yet
  linked identities included, so linking later applies the grants).
* `scim_group_role_mappings` -- the EXPLICIT organization-level configuration: group -> role. Keyed by the
  group's id, so a rename in the identity provider does not change what it grants.
* `scim_role_grants` -- one row per (group, user, role) that currently grants the role. A role granted by
  several groups has several rows; the role is only removed when the LAST one goes (and only if it is `scim`).

All four new tables are `FORCE ROW LEVEL SECURITY` with the same `tenant_isolation` policy. Deleting a group
cascades its memberships, mappings and grants.

Downgrade drops the four tables and the `granted_via` column (its values are provenance only; the role rows
themselves are untouched).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c1a5e7f3b9d2'
down_revision: str | None = 'b9f4d6e2a1c8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ('scim_groups', 'scim_group_members', 'scim_group_role_mappings', 'scim_role_grants')


def _now(name: str, nullable: bool = False):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=None if nullable else sa.text('now()'), nullable=nullable)


def upgrade() -> None:
    op.add_column('user_roles', sa.Column('granted_via', sa.Text(), server_default='manual', nullable=False))
    op.create_check_constraint('ck_user_roles_granted_via', 'user_roles', "granted_via IN ('manual', 'scim')")

    op.create_table(
        'scim_groups',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('display_name', sa.Text(), nullable=False),
        sa.Column('display_name_key', sa.Text(), nullable=False),
        sa.Column('external_id', sa.Text(), nullable=True),
        _now('created_at'),
        _now('updated_at'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_scim_groups_name', 'scim_groups', ['organization_id', 'display_name_key'], unique=True)
    op.create_index(
        'uq_scim_groups_external', 'scim_groups', ['organization_id', 'external_id'],
        unique=True, postgresql_where=sa.text('external_id IS NOT NULL'),
    )

    op.create_table(
        'scim_group_members',
        sa.Column('group_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('scim_identity_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        _now('added_at'),
        sa.ForeignKeyConstraint(['group_id'], ['scim_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['scim_identity_id'], ['scim_identities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('group_id', 'scim_identity_id'),
    )
    op.create_index('ix_scim_group_members_identity', 'scim_group_members', ['scim_identity_id'])

    op.create_table(
        'scim_group_role_mappings',
        sa.Column('group_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('role_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True),
        _now('created_at'),
        sa.ForeignKeyConstraint(['group_id'], ['scim_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('group_id', 'role_id'),
    )

    op.create_table(
        'scim_role_grants',
        sa.Column('group_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('role_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        _now('granted_at'),
        sa.ForeignKeyConstraint(['group_id'], ['scim_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('group_id', 'user_id', 'role_id'),
    )
    op.create_index('ix_scim_role_grants_user_role', 'scim_role_grants', ['organization_id', 'user_id', 'role_id'])

    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON public.{table} "
            "USING (organization_id = current_setting('app.current_organization_id', true)::uuid)"
        )


def downgrade() -> None:
    op.drop_index('ix_scim_role_grants_user_role', table_name='scim_role_grants')
    op.drop_table('scim_role_grants')
    op.drop_table('scim_group_role_mappings')
    op.drop_index('ix_scim_group_members_identity', table_name='scim_group_members')
    op.drop_table('scim_group_members')
    op.drop_index('uq_scim_groups_external', table_name='scim_groups')
    op.drop_index('uq_scim_groups_name', table_name='scim_groups')
    op.drop_table('scim_groups')
    op.drop_constraint('ck_user_roles_granted_via', 'user_roles', type_='check')
    op.drop_column('user_roles', 'granted_via')
