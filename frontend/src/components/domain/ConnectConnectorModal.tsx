import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Tabs } from "@/components/ui/Tabs";
import type { GithubRepoConfig } from "@/types/connector";

interface ConnectConnectorModalProps {
  open: boolean;
  onClose: () => void;
  onSubmitGithub: (token: string, repos: GithubRepoConfig[]) => Promise<void>;
  onSubmitSlack: (token: string, channelIds: string[]) => Promise<void>;
  onSubmitJira: (token: string, baseUrl: string, projects: string[]) => Promise<void>;
  onSubmitConfluence: (token: string, baseUrl: string, spaces: string[]) => Promise<void>;
  onSubmitTeams: (token: string, teamId: string, channels: string[]) => Promise<void>;
  onSubmitAzureDevOps: (token: string, organization: string, projects: string[]) => Promise<void>;
  onSubmitSharePoint: (token: string, siteIds: string[]) => Promise<void>;
  isSubmitting: boolean;
}

type SourceTab = "github" | "slack" | "jira" | "confluence" | "teams" | "azure_devops" | "sharepoint";

/** Shared list-of-string-keys editor: Slack channel IDs, Jira project keys,
 * and Confluence space keys are all the same shape (a token field plus a
 * dynamic list of short identifiers) -- one editor avoids four near-
 * identical copies of the same add/remove-row logic. */
function KeyListField({
  legend,
  placeholder,
  ariaLabelPrefix,
  values,
  onChange,
}: {
  legend: string;
  placeholder: string;
  ariaLabelPrefix: string;
  values: string[];
  onChange: (values: string[]) => void;
}) {
  return (
    <fieldset className="m-0 min-w-0 border-0 p-0">
      <legend className="mb-1.5 block text-xs font-medium text-ink-muted">{legend}</legend>
      <div className="flex flex-col gap-2">
        {values.map((value, index) => (
          <div key={index} className="flex gap-2">
            <Input
              placeholder={placeholder}
              aria-label={`${ariaLabelPrefix} ${index + 1}`}
              value={value}
              onChange={(e) => onChange(values.map((v, i) => (i === index ? e.target.value : v)))}
              className="flex-1"
            />
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label={`Remove ${ariaLabelPrefix.toLowerCase()} ${index + 1}`}
              onClick={() => onChange(values.filter((_, i) => i !== index))}
              disabled={values.length === 1}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        ))}
      </div>
      <Button type="button" variant="ghost" size="sm" className="mt-2 gap-1.5" onClick={() => onChange([...values, ""])}>
        <Plus className="h-3.5 w-3.5" />
        Add
      </Button>
    </fieldset>
  );
}

export function ConnectConnectorModal({
  open,
  onClose,
  onSubmitGithub,
  onSubmitSlack,
  onSubmitJira,
  onSubmitConfluence,
  onSubmitTeams,
  onSubmitAzureDevOps,
  onSubmitSharePoint,
  isSubmitting,
}: ConnectConnectorModalProps) {
  const [tab, setTab] = useState<SourceTab>("github");

  const [githubToken, setGithubToken] = useState("");
  const [repos, setRepos] = useState<GithubRepoConfig[]>([{ repo: "", ref: "" }]);

  const [slackToken, setSlackToken] = useState("");
  const [channelIds, setChannelIds] = useState<string[]>([""]);

  const [jiraToken, setJiraToken] = useState("");
  const [jiraBaseUrl, setJiraBaseUrl] = useState("");
  const [jiraProjects, setJiraProjects] = useState<string[]>([""]);

  const [confluenceToken, setConfluenceToken] = useState("");
  const [confluenceBaseUrl, setConfluenceBaseUrl] = useState("");
  const [confluenceSpaces, setConfluenceSpaces] = useState<string[]>([""]);

  const [teamsToken, setTeamsToken] = useState("");
  const [teamsTeamId, setTeamsTeamId] = useState("");
  const [teamsChannels, setTeamsChannels] = useState<string[]>([""]);

  const [azureDevOpsToken, setAzureDevOpsToken] = useState("");
  const [azureDevOpsOrg, setAzureDevOpsOrg] = useState("");
  const [azureDevOpsProjects, setAzureDevOpsProjects] = useState<string[]>([""]);

  const [sharePointToken, setSharePointToken] = useState("");
  const [sharePointSiteIds, setSharePointSiteIds] = useState<string[]>([""]);

  function resetAndClose() {
    setGithubToken("");
    setRepos([{ repo: "", ref: "" }]);
    setSlackToken("");
    setChannelIds([""]);
    setJiraToken("");
    setJiraBaseUrl("");
    setJiraProjects([""]);
    setConfluenceToken("");
    setConfluenceBaseUrl("");
    setConfluenceSpaces([""]);
    setTeamsToken("");
    setTeamsTeamId("");
    setTeamsChannels([""]);
    setAzureDevOpsToken("");
    setAzureDevOpsOrg("");
    setAzureDevOpsProjects([""]);
    setSharePointToken("");
    setSharePointSiteIds([""]);
    setTab("github");
    onClose();
  }

  async function handleGithubSubmit(event: React.FormEvent) {
    event.preventDefault();
    const cleanedRepos = repos
      .filter((r) => r.repo.trim().length > 0)
      .map((r) => ({ repo: r.repo.trim(), ref: r.ref?.trim() || undefined }));
    if (!githubToken.trim() || cleanedRepos.length === 0) return;
    await onSubmitGithub(githubToken.trim(), cleanedRepos);
    resetAndClose();
  }

  async function handleSlackSubmit(event: React.FormEvent) {
    event.preventDefault();
    const cleanedChannels = channelIds.map((c) => c.trim()).filter(Boolean);
    if (!slackToken.trim() || cleanedChannels.length === 0) return;
    await onSubmitSlack(slackToken.trim(), cleanedChannels);
    resetAndClose();
  }

  async function handleJiraSubmit(event: React.FormEvent) {
    event.preventDefault();
    const cleanedProjects = jiraProjects.map((p) => p.trim()).filter(Boolean);
    if (!jiraToken.trim() || !jiraBaseUrl.trim() || cleanedProjects.length === 0) return;
    await onSubmitJira(jiraToken.trim(), jiraBaseUrl.trim(), cleanedProjects);
    resetAndClose();
  }

  async function handleConfluenceSubmit(event: React.FormEvent) {
    event.preventDefault();
    const cleanedSpaces = confluenceSpaces.map((s) => s.trim()).filter(Boolean);
    if (!confluenceToken.trim() || !confluenceBaseUrl.trim() || cleanedSpaces.length === 0) return;
    await onSubmitConfluence(confluenceToken.trim(), confluenceBaseUrl.trim(), cleanedSpaces);
    resetAndClose();
  }

  async function handleTeamsSubmit(event: React.FormEvent) {
    event.preventDefault();
    const cleanedChannels = teamsChannels.map((c) => c.trim()).filter(Boolean);
    if (!teamsToken.trim() || !teamsTeamId.trim() || cleanedChannels.length === 0) return;
    await onSubmitTeams(teamsToken.trim(), teamsTeamId.trim(), cleanedChannels);
    resetAndClose();
  }

  async function handleAzureDevOpsSubmit(event: React.FormEvent) {
    event.preventDefault();
    const cleanedProjects = azureDevOpsProjects.map((p) => p.trim()).filter(Boolean);
    if (!azureDevOpsToken.trim() || !azureDevOpsOrg.trim() || cleanedProjects.length === 0) return;
    await onSubmitAzureDevOps(azureDevOpsToken.trim(), azureDevOpsOrg.trim(), cleanedProjects);
    resetAndClose();
  }

  async function handleSharePointSubmit(event: React.FormEvent) {
    event.preventDefault();
    const cleanedSiteIds = sharePointSiteIds.map((s) => s.trim()).filter(Boolean);
    if (!sharePointToken.trim() || cleanedSiteIds.length === 0) return;
    await onSubmitSharePoint(sharePointToken.trim(), cleanedSiteIds);
    resetAndClose();
  }

  return (
    <Modal
      open={open}
      onClose={resetAndClose}
      title="Connect a source"
      description="Credentials are envelope-encrypted at rest and never displayed again after saving."
      className="max-w-xl"
    >
      <div className="mb-4">
        <Tabs
          items={[
            { key: "github", label: "GitHub" },
            { key: "slack", label: "Slack" },
            { key: "jira", label: "Jira" },
            { key: "confluence", label: "Confluence" },
            { key: "teams", label: "Teams" },
            { key: "azure_devops", label: "Azure DevOps" },
            { key: "sharepoint", label: "SharePoint" },
          ]}
          activeKey={tab}
          onChange={(key) => setTab(key as SourceTab)}
          idPrefix="connect-source"
        />
      </div>

      {tab === "github" && (
        <form onSubmit={handleGithubSubmit} className="flex flex-col gap-3">
          <div>
            <label htmlFor="github-token" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Personal access token
            </label>
            <Input
              id="github-token"
              type="password"
              required
              placeholder="ghp_…"
              value={githubToken}
              onChange={(e) => setGithubToken(e.target.value)}
            />
          </div>

          <fieldset className="m-0 min-w-0 border-0 p-0">
            <legend className="mb-1.5 block text-xs font-medium text-ink-muted">Repositories</legend>
            <div className="flex flex-col gap-2">
              {repos.map((row, index) => (
                <div key={index} className="flex gap-2">
                  <Input
                    placeholder="owner/repo"
                    aria-label={`Repository ${index + 1} (owner/repo)`}
                    value={row.repo}
                    onChange={(e) =>
                      setRepos((prev) =>
                        prev.map((r, i) => (i === index ? { ...r, repo: e.target.value } : r)),
                      )
                    }
                    className="flex-1"
                  />
                  <Input
                    placeholder="branch (default: main)"
                    aria-label={`Repository ${index + 1} branch (default: main)`}
                    value={row.ref ?? ""}
                    onChange={(e) =>
                      setRepos((prev) =>
                        prev.map((r, i) => (i === index ? { ...r, ref: e.target.value } : r)),
                      )
                    }
                    className="w-40"
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label="Remove repository"
                    onClick={() => setRepos((prev) => prev.filter((_, i) => i !== index))}
                    disabled={repos.length === 1}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
            </div>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="mt-2 gap-1.5"
              onClick={() => setRepos((prev) => [...prev, { repo: "", ref: "" }])}
            >
              <Plus className="h-3.5 w-3.5" />
              Add repository
            </Button>
          </fieldset>

          <Button type="submit" variant="primary" isLoading={isSubmitting} className="mt-2">
            Connect GitHub
          </Button>
        </form>
      )}

      {tab === "slack" && (
        <form onSubmit={handleSlackSubmit} className="flex flex-col gap-3">
          <div>
            <label htmlFor="slack-token" className="mb-1.5 block text-xs font-medium text-ink-muted">Bot token</label>
            <Input
              id="slack-token"
              type="password"
              required
              placeholder="xoxb-…"
              value={slackToken}
              onChange={(e) => setSlackToken(e.target.value)}
            />
          </div>

          <KeyListField
            legend="Channel IDs"
            placeholder="C0123456789"
            ariaLabelPrefix="Channel"
            values={channelIds}
            onChange={setChannelIds}
          />

          <Button type="submit" variant="primary" isLoading={isSubmitting} className="mt-2">
            Connect Slack
          </Button>
        </form>
      )}

      {tab === "jira" && (
        <form onSubmit={handleJiraSubmit} className="flex flex-col gap-3">
          <div>
            <label htmlFor="jira-token" className="mb-1.5 block text-xs font-medium text-ink-muted">API token</label>
            <Input
              id="jira-token"
              type="password"
              required
              placeholder="ATATT3…"
              value={jiraToken}
              onChange={(e) => setJiraToken(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="jira-base-url" className="mb-1.5 block text-xs font-medium text-ink-muted">Site URL</label>
            <Input
              id="jira-base-url"
              type="url"
              required
              placeholder="https://acme.atlassian.net"
              value={jiraBaseUrl}
              onChange={(e) => setJiraBaseUrl(e.target.value)}
            />
          </div>

          <KeyListField
            legend="Project keys"
            placeholder="OPS"
            ariaLabelPrefix="Project key"
            values={jiraProjects}
            onChange={setJiraProjects}
          />

          <Button type="submit" variant="primary" isLoading={isSubmitting} className="mt-2">
            Connect Jira
          </Button>
        </form>
      )}

      {tab === "confluence" && (
        <form onSubmit={handleConfluenceSubmit} className="flex flex-col gap-3">
          <div>
            <label htmlFor="confluence-token" className="mb-1.5 block text-xs font-medium text-ink-muted">API token</label>
            <Input
              id="confluence-token"
              type="password"
              required
              placeholder="ATATT3…"
              value={confluenceToken}
              onChange={(e) => setConfluenceToken(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="confluence-base-url" className="mb-1.5 block text-xs font-medium text-ink-muted">Site URL</label>
            <Input
              id="confluence-base-url"
              type="url"
              required
              placeholder="https://acme.atlassian.net"
              value={confluenceBaseUrl}
              onChange={(e) => setConfluenceBaseUrl(e.target.value)}
            />
          </div>

          <KeyListField
            legend="Space keys"
            placeholder="ENG"
            ariaLabelPrefix="Space key"
            values={confluenceSpaces}
            onChange={setConfluenceSpaces}
          />

          <Button type="submit" variant="primary" isLoading={isSubmitting} className="mt-2">
            Connect Confluence
          </Button>
        </form>
      )}

      {tab === "teams" && (
        <form onSubmit={handleTeamsSubmit} className="flex flex-col gap-3">
          <div>
            <label htmlFor="teams-token" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Graph API access token
            </label>
            <Input
              id="teams-token"
              type="password"
              required
              placeholder="Bearer token from your app registration"
              value={teamsToken}
              onChange={(e) => setTeamsToken(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="teams-team-id" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Team ID
            </label>
            <Input
              id="teams-team-id"
              required
              placeholder="19:abcd1234…@thread.tacv2"
              value={teamsTeamId}
              onChange={(e) => setTeamsTeamId(e.target.value)}
            />
          </div>

          <KeyListField
            legend="Channel IDs"
            placeholder="19:xyz9876…@thread.tacv2"
            ariaLabelPrefix="Channel"
            values={teamsChannels}
            onChange={setTeamsChannels}
          />

          <Button type="submit" variant="primary" isLoading={isSubmitting} className="mt-2">
            Connect Teams
          </Button>
        </form>
      )}

      {tab === "azure_devops" && (
        <form onSubmit={handleAzureDevOpsSubmit} className="flex flex-col gap-3">
          <div>
            <label htmlFor="azure-devops-token" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Personal access token
            </label>
            <Input
              id="azure-devops-token"
              type="password"
              required
              placeholder="PAT with Work Items (Read) scope"
              value={azureDevOpsToken}
              onChange={(e) => setAzureDevOpsToken(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="azure-devops-org" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Organization
            </label>
            <Input
              id="azure-devops-org"
              required
              placeholder="acme-corp"
              value={azureDevOpsOrg}
              onChange={(e) => setAzureDevOpsOrg(e.target.value)}
            />
          </div>

          <KeyListField
            legend="Project names"
            placeholder="Platform"
            ariaLabelPrefix="Project"
            values={azureDevOpsProjects}
            onChange={setAzureDevOpsProjects}
          />

          <Button type="submit" variant="primary" isLoading={isSubmitting} className="mt-2">
            Connect Azure DevOps
          </Button>
        </form>
      )}

      {tab === "sharepoint" && (
        <form onSubmit={handleSharePointSubmit} className="flex flex-col gap-3">
          <div>
            <label htmlFor="sharepoint-token" className="mb-1.5 block text-xs font-medium text-ink-muted">
              Graph API access token
            </label>
            <Input
              id="sharepoint-token"
              type="password"
              required
              placeholder="Bearer token from your app registration"
              value={sharePointToken}
              onChange={(e) => setSharePointToken(e.target.value)}
            />
          </div>

          <KeyListField
            legend="Site IDs"
            placeholder="contoso.sharepoint.com,abcd1234-…"
            ariaLabelPrefix="Site"
            values={sharePointSiteIds}
            onChange={setSharePointSiteIds}
          />

          <Button type="submit" variant="primary" isLoading={isSubmitting} className="mt-2">
            Connect SharePoint
          </Button>
        </form>
      )}
    </Modal>
  );
}
