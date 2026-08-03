"""Manual, standalone smoke test for the Slack and GitHub ingestion
connectors (app/ingestion/connectors/slack.py, github.py).

Exercises `authenticate` -> `fetch_batch` -> `normalize` directly, with no
database, no organization/project rows, and no ingestion job runner (task
#12, not yet built) -- connectors only need a `ResolvedConnectorConfig`
object, which this script builds by hand with throwaway UUIDs. Prints what
it fetched so you can eyeball the result.

Not a pytest test: it makes real network calls against your own Slack
workspace / GitHub account and needs real personal credentials, so it isn't
something CI should run automatically. Lives in scripts/, not tests/, for
exactly that reason.

Setup, no company/employer needed for either:
  Slack  -- create your own workspace (free) + an app at
            api.slack.com/apps, add Bot Token Scopes `channels:history` and
            `channels:read`, install it, invite the bot to a channel
            (`/invite @your-bot`), post a couple of test messages.
  GitHub -- create a personal access token at github.com/settings/tokens
            (classic, `repo` scope, or a fine-grained token scoped to one
            repo) -- works against any repo you can read, including public
            ones.

Reads credentials from environment variables, loaded from a `.env` file in
the repo root if present (via python-dotenv, already a transitive
dependency of pydantic-settings -- see app/shared/config/settings.py).
Never hardcode a real token into this file.

  EKIP_TEST_SLACK_BOT_TOKEN
  EKIP_TEST_SLACK_CHANNEL_ID
  EKIP_TEST_GITHUB_TOKEN
  EKIP_TEST_GITHUB_REPO         e.g. "octocat/Hello-World"
  EKIP_TEST_GITHUB_REF          optional, default "main"

Either pair may be omitted -- this script skips whichever connector doesn't
have its credentials set, rather than failing outright.

Run: python scripts/test_connectors.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

from dotenv import load_dotenv

from app.ingestion.connectors.github import GitHubConnector
from app.ingestion.connectors.slack import SlackConnector
from app.ingestion.schemas import ResolvedConnectorConfig

load_dotenv()


async def test_slack() -> None:
    token = os.environ.get("EKIP_TEST_SLACK_BOT_TOKEN")
    channel = os.environ.get("EKIP_TEST_SLACK_CHANNEL_ID")
    if not token or not channel:
        print("Skipping Slack: set EKIP_TEST_SLACK_BOT_TOKEN and EKIP_TEST_SLACK_CHANNEL_ID.")
        return

    print("\n=== Slack ===")
    connector = SlackConnector()
    config = ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=None,
        source="slack",
        credential_ref=token,
        config={"channels": [channel]},
    )
    client = await connector.authenticate(config)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        print(f"Fetched {len(result.items)} raw message(s), has_more={result.has_more}")
        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            print(f"  - [{doc.external_id}] {doc.content[:80]!r} metadata={doc.metadata}")
    finally:
        await connector.close(client)


async def test_github() -> None:
    token = os.environ.get("EKIP_TEST_GITHUB_TOKEN")
    repo = os.environ.get("EKIP_TEST_GITHUB_REPO")
    if not token or not repo:
        print("Skipping GitHub: set EKIP_TEST_GITHUB_TOKEN and EKIP_TEST_GITHUB_REPO.")
        return

    print("\n=== GitHub ===")
    ref = os.environ.get("EKIP_TEST_GITHUB_REF", "main")
    connector = GitHubConnector()
    config = ResolvedConnectorConfig(
        connector_config_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=None,
        source="github",
        credential_ref=token,
        config={"repos": [{"repo": repo, "ref": ref}]},
    )
    client = await connector.authenticate(config)
    try:
        result = await connector.fetch_batch(client, since=None, cursor=None)
        print(f"Fetched {len(result.items)} raw file(s), has_more={result.has_more}")
        for raw_item in result.items:
            doc = connector.normalize(raw_item)
            print(f"  - [{doc.external_id}] {len(doc.content)} chars, url={doc.source_url}")
    finally:
        await connector.close(client)


async def main() -> None:
    await test_slack()
    await test_github()


if __name__ == "__main__":
    asyncio.run(main())
