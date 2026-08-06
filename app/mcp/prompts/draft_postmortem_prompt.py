"""MCP prompt: `draft-postmortem` (API_DESIGN.md section 3: "wraps
`generate_postmortem`").

Pure text template -- see `app.mcp.prompts`'s module docstring. Only
instructs the client's LLM to call the real `generate_postmortem` tool
(`app.mcp.tools.generate_postmortem`).
"""

from __future__ import annotations

from app.mcp.servers.server import mcp_server


@mcp_server.prompt(name="draft-postmortem")
def draft_postmortem_prompt(incident_id: str) -> str:
    """Frame a postmortem-drafting request for `incident_id`."""
    return (
        f"Draft a postmortem for incident {incident_id}. Call the "
        "`generate_postmortem` tool with this incident_id -- it persists a "
        "new draft postmortem (root cause + action items) reconstructed from "
        "the incident's timeline. Present the result explicitly as a DRAFT "
        "requiring human review and approval (via the incident owner's "
        "`/postmortems/{id}/approve` action) before it is treated as final "
        "-- never present a freshly generated draft as an already-approved "
        "conclusion."
    )
