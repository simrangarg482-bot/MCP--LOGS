[CmdletBinding()]
param(
    [ValidateSet("Preflight", "Up", "Seed", "Rls", "E2E", "All", "Down")]
    [string]$Phase = "Preflight",
    [switch]$Fresh,
    [switch]$IncludeConnector,
    [switch]$RemoveData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ComposeProject = "ekip-real-e2e"
$ComposeFiles = @(
    "-f", (Join-Path $RepoRoot "docker-compose.yml"),
    "-f", (Join-Path $RepoRoot "docker-compose.real-test.yml")
)
$RunStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$EvidenceRoot = Join-Path $RepoRoot ".real-e2e-results"
$EvidenceDir = Join-Path $EvidenceRoot $RunStamp
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-Logged {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$CommandArgs,
        [Parameter(Mandatory = $true)][string]$LogName,
        [switch]$AllowFailure
    )

    $logPath = Join-Path $EvidenceDir $LogName
    & $Command @CommandArgs 2>&1 | Tee-Object -FilePath $logPath
    $exitCode = $LASTEXITCODE
    if (-not $AllowFailure -and $exitCode -ne 0) {
        throw "$Command failed with exit code $exitCode. Evidence: $logPath"
    }
    return $exitCode
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)][string[]]$CommandArgs,
        [Parameter(Mandatory = $true)][string]$LogName,
        [switch]$AllowFailure
    )
    $allArgs = @("compose", "-p", $ComposeProject) + $ComposeFiles + $CommandArgs
    return Invoke-Logged -Command "docker" -CommandArgs $allArgs -LogName $LogName -AllowFailure:$AllowFailure
}

function Get-DotEnvValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^$([regex]::Escape($Name))=(.*)$") {
            return $Matches[1].Trim()
        }
    }
    return $null
}

function Wait-Http {
    param([string]$Url, [int]$TimeoutSeconds = 240)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) { return $response.Content }
        } catch {
            # Expected while containers are starting. The final timeout below
            # reports a concise failure and preserves the running stack.
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for $Url after $TimeoutSeconds seconds."
}

function Save-Metadata {
    $metadata = @(
        "started_at=$((Get-Date).ToString('o'))",
        "phase=$Phase",
        "compose_project=$ComposeProject",
        "include_connector=$IncludeConnector",
        "fresh=$Fresh"
    )
    $metadata | Set-Content -LiteralPath (Join-Path $EvidenceDir "run-metadata.txt")
    Push-Location $RepoRoot
    try {
        git rev-parse HEAD | Set-Content -LiteralPath (Join-Path $EvidenceDir "commit.txt")
        git status --short | Set-Content -LiteralPath (Join-Path $EvidenceDir "git-status.txt")
    } finally {
        Pop-Location
    }
}

function Run-Preflight {
    Write-Step "Preflight checks"
    $failures = [System.Collections.Generic.List[string]]::new()
    foreach ($command in @("docker", "node", "npm", "npx", "git")) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            $failures.Add("Required command is missing: $command")
        }
    }
    if (-not (Test-Path -LiteralPath $Python)) {
        $failures.Add("Python virtual environment is missing: $Python (run: uv sync --extra dev)")
    }

    $dockerEnv = Join-Path $RepoRoot ".env.docker"
    if (-not (Test-Path -LiteralPath $dockerEnv)) {
        $failures.Add(".env.docker is missing. Copy .env.docker.example and set OPENAI_API_KEY.")
    } else {
        $openAiKey = Get-DotEnvValue -Path $dockerEnv -Name "OPENAI_API_KEY"
        if ([string]::IsNullOrWhiteSpace($openAiKey) -or $openAiKey -match "replace-with|your-real-key|placeholder") {
            $failures.Add("OPENAI_API_KEY in .env.docker is missing or still a placeholder.")
        }
    }

    if ($IncludeConnector) {
        $rootEnv = Join-Path $RepoRoot ".env"
        $githubToken = Get-DotEnvValue -Path $rootEnv -Name "EKIP_TEST_GITHUB_TOKEN"
        $githubRepos = Get-DotEnvValue -Path $rootEnv -Name "EKIP_TEST_GITHUB_REPOS"
        if ([string]::IsNullOrWhiteSpace($githubToken) -or [string]::IsNullOrWhiteSpace($githubRepos)) {
            $failures.Add("Connector testing requires EKIP_TEST_GITHUB_TOKEN and EKIP_TEST_GITHUB_REPOS in the gitignored root .env.")
        }
    }

    if (Get-Command docker -ErrorAction SilentlyContinue) {
        & docker info *> $null
        if ($LASTEXITCODE -ne 0) {
            $failures.Add("Docker is installed but its daemon is not running. Start Docker Desktop first.")
        }
    }

    if ($failures.Count -gt 0) {
        $failures | ForEach-Object { Write-Host "FAIL: $_" -ForegroundColor Red }
        throw "Preflight failed with $($failures.Count) issue(s)."
    }

    Push-Location (Join-Path $RepoRoot "frontend")
    try {
        Invoke-Logged -Command "npx" -CommandArgs @("playwright", "test", "--list") -LogName "playwright-list.txt" | Out-Null
    } finally {
        Pop-Location
    }

    Invoke-Logged -Command "docker" -CommandArgs @("version") -LogName "docker-version.txt" | Out-Null
    Write-Host "Preflight passed." -ForegroundColor Green
}

function Stop-TestStack {
    param([switch]$WithData)
    Write-Step "Stopping isolated compose project $ComposeProject"
    $downArgs = @("down", "--remove-orphans")
    if ($WithData) {
        # This removes only volumes belonging to the explicit
        # `ekip-real-e2e` compose project, never another compose project.
        $downArgs += "--volumes"
    }
    Invoke-Compose -CommandArgs $downArgs -LogName "compose-down.txt" -AllowFailure | Out-Null
}

function Start-TestStack {
    if ($Fresh) {
        Stop-TestStack -WithData
    }
    Write-Step "Building and starting the production-shaped test stack"
    Invoke-Compose -CommandArgs @("up", "--build", "--detach") -LogName "compose-up.txt" | Out-Null

    $health = Wait-Http -Url "http://127.0.0.1:8000/health"
    $health | Set-Content -LiteralPath (Join-Path $EvidenceDir "health.json")
    $ready = Wait-Http -Url "http://127.0.0.1:8000/ready"
    $ready | Set-Content -LiteralPath (Join-Path $EvidenceDir "ready.json")
    Wait-Http -Url "http://127.0.0.1:8080/" | Out-Null

    Invoke-Compose -CommandArgs @("ps", "--all") -LogName "compose-ps.txt" | Out-Null
    Write-Host "Stack is healthy: frontend http://127.0.0.1:8080, API http://127.0.0.1:8000" -ForegroundColor Green
}

function Seed-TestData {
    Write-Step "Seeding two tenants, RBAC users, and review fixtures"
    $adminDatabaseUrl = "postgresql+asyncpg://ekip:ekip_local_dev_only@postgres:5432/ekip"
    Invoke-Compose -CommandArgs @(
        "run", "--rm", "--no-deps",
        "-e", "DATABASE_URL=$adminDatabaseUrl",
        "backend", "python", "scripts/e2e_seed.py"
    ) -LogName "seed.txt" | Out-Null
}

function Run-RlsProof {
    Write-Step "Running real PostgreSQL RLS isolation proof"
    $rlsDatabaseUrl = "postgresql://ekip:ekip_local_dev_only@postgres:5432/ekip"
    Invoke-Compose -CommandArgs @(
        "run", "--rm", "--no-deps",
        "-e", "RLS_TEST_DATABASE_URL=$rlsDatabaseUrl",
        "backend", "python", "scripts/rls_isolation_test.py"
    ) -LogName "rls-isolation.txt" | Out-Null
}

function Run-Playwright {
    Write-Step "Running 32 browser E2E tests against the compose stack"
    Push-Location (Join-Path $RepoRoot "frontend")
    try {
        $env:E2E_BASE_URL = "http://127.0.0.1:8080"
        $env:E2E_API_BASE_URL = "http://127.0.0.1:8000"
        Invoke-Logged -Command "npx" -CommandArgs @("playwright", "install", "chromium") -LogName "playwright-browser-install.txt" | Out-Null
        Invoke-Logged -Command "npx" -CommandArgs @("playwright", "test", "--reporter=list,html") -LogName "playwright.txt" | Out-Null
    } finally {
        Pop-Location
    }

    foreach ($artifact in @("e2e-report", "test-results")) {
        $source = Join-Path (Join-Path $RepoRoot "frontend") $artifact
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $EvidenceDir $artifact) -Recurse -Force
        }
    }

    if ($IncludeConnector) {
        Write-Step "Running controlled-corpus answer-quality canaries"
        $env:EKIP_E2E_PASSWORD = "E2eTest123!"
        Invoke-Logged -Command $Python -CommandArgs @(
            "scripts/real_e2e_quality.py",
            "--api-base", "http://127.0.0.1:8000",
            "--dataset", "tests/real_e2e/quality_cases.example.json",
            "--report", (Join-Path $EvidenceDir "answer-quality.json")
        ) -LogName "answer-quality.txt" | Out-Null
    }
}

Save-Metadata
Push-Location $RepoRoot
try {
    switch ($Phase) {
        "Preflight" { Run-Preflight }
        "Up" { Run-Preflight; Start-TestStack }
        "Seed" { Seed-TestData }
        "Rls" { Run-RlsProof }
        "E2E" { Run-Playwright }
        "Down" { Stop-TestStack -WithData:$RemoveData }
        "All" {
            Run-Preflight
            Start-TestStack
            Seed-TestData
            Run-RlsProof
            Run-Playwright
        }
    }
    Write-Host "`nPhase '$Phase' completed. Evidence: $EvidenceDir" -ForegroundColor Green
} catch {
    Write-Host "`nPhase '$Phase' failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "The stack is intentionally left running for diagnosis. Evidence: $EvidenceDir" -ForegroundColor Yellow
    exit 1
} finally {
    Pop-Location
}
