# Deploy the current main branch to production (iansbookrecs.com).
#
# Usage:  .\scripts\deploy.ps1            # run tests, then deploy
#         .\scripts\deploy.ps1 -SkipTests # deploy without running tests
#
# Safe to keep in the public repo: it references the SSH alias "iansbookrecs",
# which resolves via the operator's local ~/.ssh/config (host, user, and key
# path live there — never in the repo). Anyone else running this just gets an
# SSH error.
#
# What it does: test locally -> verify everything is pushed -> pull + rebuild
# on the server -> hit the live site to confirm it came back up.

param([switch]$SkipTests)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not $SkipTests) {
    Write-Host "[1/4] Running tests..." -ForegroundColor Cyan
    python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed - not deploying." }
} else {
    Write-Host "[1/4] Skipping tests." -ForegroundColor Yellow
}

Write-Host "[2/4] Checking everything is committed and pushed..." -ForegroundColor Cyan
$dirty = git status --porcelain
if ($dirty) { throw "Uncommitted changes - commit and push first.`n$dirty" }
git fetch -q origin
$unpushed = git log origin/main..HEAD --oneline
if ($unpushed) { throw "Unpushed commits - run 'git push' first.`n$unpushed" }

Write-Host "[3/4] Deploying on the server (pull + rebuild)..." -ForegroundColor Cyan
# The trailing 2>&1 runs on the REMOTE shell: git/docker write progress to
# stderr, which Windows PowerShell 5.1 would otherwise convert into a
# terminating NativeCommandError and kill the deploy mid-flight.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
ssh iansbookrecs "(cd app && git pull && sudo docker compose -f docker-compose.prod.yml up -d --build) 2>&1"
$ErrorActionPreference = $prevEAP
if ($LASTEXITCODE -ne 0) { throw "Remote deploy failed." }

Write-Host "[4/4] Verifying the live site..." -ForegroundColor Cyan
Start-Sleep -Seconds 5
$r = Invoke-WebRequest -Uri "https://iansbookrecs.com" -UseBasicParsing -TimeoutSec 30
if ($r.StatusCode -eq 200) {
    Write-Host "Deployed - https://iansbookrecs.com is live (HTTP 200)." -ForegroundColor Green
} else {
    throw "Site responded with HTTP $($r.StatusCode) - check 'ssh iansbookrecs' and the container logs."
}
