"""MCP prompt: `triage-incident` (API_DESIGN.md section 3: "wraps
`investigate_incident`, pre-filled with a triage-oriented system framing").

Pure text template -- see `app.mcp.prompts`'s module docstring for why this
does not resolve an `Identity` or call `run_mcp_tool` itself: it only
instructs the client's LLM to call the real `investigate_incident` tool
(`app.mcp.tools.investigate_incident`), which is what actually authenticates
and runs the Investigation Agent.
"""

from __future__ import annotations

from app.mcp.servers.server import mcp_server


@mcp_server.prompt(name="triage-incident")
def triage_incident_prompt(incident_id: str) -> str:
    """Frame a triage request for `incident_id`, directing the model at the
    `investigate_incident` tool rather than a general-purpose answer.
    """
    return (
        f"An incident (id: {incident_id}) needs triage. Call the "
        "`investigate_incident` tool with this incident_id to gather verified "
        "evidence (recent commits/PRs, Slack conversations, related "
        "postmortems) and AI-generated root-cause hypotheses. When you "
        "respond, keep the Investigation Agent's own distinction intact: "
        "present `evidence` as verified fact and `hypotheses` explicitly as "
        "generated, unconfirmed reasoning -- never merge the two into a "
        "single confident-sounding claim. Recommend `suggested_owner_team` "
        "and `suggested_next_steps` from the tool's response as your "
        "concrete next actions."
    )
