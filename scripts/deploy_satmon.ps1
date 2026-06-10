param(
    [string]$HostName = "satmon",
    [string]$RemoteDir = "/mnt/competegraph",
    [int]$BackendPort = 18000,
    [int]$FrontendPort = 18080,
    [string]$PublicHost = "8.136.33.172"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Archive = Join-Path $env:TEMP "competegraph_$Stamp.tar.gz"
$RemoteArchive = "/tmp/competegraph_$Stamp.tar.gz"
$RemoteInstaller = "/tmp/install_competegraph_remote_$Stamp.sh"
$Installer = Join-Path $PSScriptRoot "install_competegraph_remote.sh"

function Assert-Command($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found."
    }
}

Assert-Command "ssh"
Assert-Command "scp"
Assert-Command "tar"
Assert-Command "pnpm"

if (-not (Test-Path (Join-Path $RepoRoot "backend\.env"))) {
    throw "backend\.env is required for production deployment."
}

if (-not (Test-Path $Installer)) {
    throw "Remote installer not found: $Installer"
}

Push-Location (Join-Path $RepoRoot "frontend")
try {
    pnpm build
}
finally {
    Pop-Location
}

Push-Location $RepoRoot
try {
    if (Test-Path $Archive) {
        Remove-Item -LiteralPath $Archive -Force
    }

    tar `
        --exclude="backend/.venv" `
        --exclude="backend/.pytest_cache" `
        --exclude="frontend/node_modules" `
        --exclude="frontend/.vite" `
        --exclude="frontend/*.tsbuildinfo" `
        --exclude=".git" `
        --exclude=".claude" `
        -czf $Archive `
        backend/cg `
        backend/pyproject.toml `
        backend/uv.lock `
        backend/.env `
        frontend/dist `
        scripts/init_data.py `
        scripts/regenerate_report.py `
        skills
}
finally {
    Pop-Location
}

Write-Host "Uploading archive to ${HostName}:$RemoteArchive"
scp $Archive "${HostName}:$RemoteArchive"
scp $Installer "${HostName}:$RemoteInstaller"

Write-Host "Running remote installer on $HostName"
ssh $HostName "bash '$RemoteInstaller' '$RemoteArchive' '$RemoteDir' '$BackendPort' '$FrontendPort'"

Write-Host ""
Write-Host "Deployment complete:"
if ($FrontendPort -eq 80) {
    Write-Host "  http://$PublicHost/"
    Write-Host "  http://$PublicHost/health"
}
else {
    Write-Host "  http://${PublicHost}:$FrontendPort/"
    Write-Host "  http://${PublicHost}:$FrontendPort/health"
}
