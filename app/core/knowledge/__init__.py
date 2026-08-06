"""core/knowledge -- the proposed-document review lifecycle.

Owned by: core/knowledge. Backs API_DESIGN.md section 1's "Knowledge review
queue" REST endpoints and section 3's `propose_runbook_update` MCP tool /
`document://` MCP resource -- all three were previously unbuilt/unwired
because no core-owned function existed to create or read a `documents` row
outside `app.ingestion`'s connector-sync pipeline. See `service.py`'s module
docstring for the full design and `repository.py`'s for the specific
data-access tradeoffs this closes.
"""

from __future__ import annotations
