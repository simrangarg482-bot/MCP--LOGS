# RBAC read-path authorization audit (Phase 4.7.1 / 4.7.4)

Full audit of every service-layer function that reads or writes tenant-scoped
data, cross-referenced against `app/core/users/service.py`'s authorization
primitives (`require_permission`, `require_project_permission`,
`has_permission`, `authorize`) and the org-membership-only guard
(`_ensure_same_organization`-style checks). Confirms MCP and REST never run a
separate authorization path — both always resolve the same `Identity` and
call straight into `core`/`agents` service functions.

The full permission catalog on `main` before this phase: `tenancy:manage`,
`incident:write`, `postmortem:write`, `postmortem:approve`, `knowledge:review`,
`observability:read`, `audit:read` (`app/core/users/repository.py:
ADMIN_PERMISSION_CODES`). `incident:read` is added by this phase
(`d706a360fc2a`); there is still no `document:read`/`project:read`/
`connector:read` code, by design — see the "already protected" rows below for
how those resources are actually gated instead.

## Confirmed vulnerabilities — fixed this phase

| Function | Endpoint(s) | Was | Now | Fix |
|---|---|---|---|---|
| `app.core.incidents.service.get_incident` | `GET /incidents/{id}`, MCP `incident://{id}` | `_ensure_same_organization` only | `require_project_permission(actor, incident.project_id, "incident:read")` after ownership check | Code + `d706a360fc2a` migration (seed + backfill) |
| `app.core.incidents.service.list_incidents` | `GET /incidents` | `_ensure_same_organization` only | `require_permission(actor, "incident:read")` (org-level — no project scope exists on `IncidentFilter`) | Same |
| `app.core.incidents.service.get_timeline` | `GET /incidents/{id}/timeline` | `_ensure_same_organization` + ownership only | `require_project_permission(actor, incident.project_id, "incident:read")` | Same |
| `app.agents.service.triage_incident` | `POST /incidents/{id}/investigate`, MCP `investigate_incident` | Inherited the `get_incident` gap transitively (calls it as its own authorization boundary) — any org member could trigger a full LLM investigation of any incident | Closed automatically — no separate code change needed, since it calls the now-fixed `get_incident` | Same (transitive) |

Regression tests: `tests/core/incidents/test_service.py` — 10 new tests
covering same-org+permission=allow (project-scoped and org-level fallback),
same-org+no-permission=deny, cross-org=deny (before the permission check even
runs), and nonexistent-id=404 regardless of permission. All 428 backend
tests pass; 7/7 import-linter contracts kept.

**E2E fixture updated to match**: `scripts/e2e_seed.py`'s
`RESTRICTED_PERMISSION_CODES` (the `e2e_incident_writer` role) now includes
`incident:read` alongside `incident:write` — `frontend/e2e/rbac.spec.ts`'s
"a restricted user can create an incident and view it back" test navigates
to the incident it just created immediately after creating it, which would
otherwise now be denied by this same fix. **Not re-verified by an actual
Playwright run** — no live stack in this environment; reasoned from reading
the spec and the fix's logic, flagged honestly rather than assumed passing.

## Already protected (no action needed)

| Function | Check | Note |
|---|---|---|
| `get_postmortem` / `get_postmortem_by_incident` | draft/in_review requires `postmortem:write` or `postmortem:approve`; approved/published open to org members | Own docstring states this was already fixed previously — a different, working design; no `postmortem:read` gap exists to close |
| `core.knowledge.service.get_document` / `list_proposed_documents` | `knowledge:review` (project-scoped) for proposed docs | Correct precedent for what `get_incident` should have had |
| `list_published_documents` | org-membership only, explicitly documented as intentional | Consistent, well-reasoned |
| `core.tenancy.service.list_connectors` / `list_access_rules` / `list_invitations` | `tenancy:manage` | Each docstring states it was *already* fixed for this exact bug class previously |
| `core.audit.service.query_audit_log` | `audit:read` | Docstring: closed before ever getting a caller |
| `list_organization_members` | org-membership only, explicitly documented as intentional (teammate visibility) | Good example of a properly-justified exception |
| `get_question_history` | self-scoped by `user_id`, not a permission | Correct — no org-wide exposure |

## Flagged, not fixed — product policy uncertain

Per this phase's "only fix confirmed vulnerabilities" rule, these are
documented for a human product decision, not silently changed:

- **`core.tenancy.service.list_projects`** (`GET /organizations/{id}/projects`)
  — org-membership only, while `create_project` requires `tenancy:manage`.
  Same write-gated/read-not shape as the incidents bug, but on lower-apparent-
  sensitivity data (project names) and with no docstring arguing it's
  intentional (unlike `list_organization_members`). Recommend either an
  explicit intentional-access docstring or a permission check — currently
  silent, which is exactly the shape the incidents bug had.
- **`answer_question`** (`POST /ask`) — no permission check at all; arguably
  intentional (asking questions is a baseline capability, no `ask:read`
  permission exists), but undocumented as a deliberate choice.
- **`list_recent_postmortems`** — its own docstring justifies itself by
  citing `get_incident`/`list_incidents` as precedent ("matching
  `get_incident`/`list_incidents`") — precedent that was, until this phase,
  itself the bug. Low risk in practice (only ever returns already-reviewed
  postmortems), but the stated justification should be re-examined/reworded
  now that the precedent it cites has changed.
- **`search_similar_incidents` / `search_recent_changes`** — not a
  vulnerability (fails closed: empty `permission_codes` hides ACL-tagged
  content rather than leaking it), but inconsistent with
  `agents/retrieval/node.py` and `agents/investigation/evidence.py`, which
  both populate `permission_codes=actor.permissions`. Worth aligning for
  correctness (a user entitled to see ACL'd chunks via other paths currently
  won't see them here), not for security.

## MCP / router delegation

Confirmed clean across every tool/resource file and router spot-checked:
zero MCP-specific authorization logic beyond `extract_bearer_token` +
schema validation; `organization_id` is always sourced from the resolved
`actor.organization_id`, never a client-supplied override. The gaps above
are exposed identically via REST and MCP — there is no separate MCP-side fix
needed.
