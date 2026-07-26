# Push the whole FireProtect project to GitHub.
#
#   Right-click this file -> "Run with PowerShell"
#   ...or from a PowerShell prompt:
#       cd D:\My\FireProtect
#       powershell -ExecutionPolicy Bypass -File .\push_to_github.ps1
#
# It stages everything, commits, and pushes. If anything is wrong it prints
# the reason instead of failing silently.

$ErrorActionPreference = "Continue"

# Always operate on the folder this script lives in, never the caller's cwd.
Set-Location -Path $PSScriptRoot

function Section($text) {
    Write-Host ""
    Write-Host "=== $text ===" -ForegroundColor Cyan
}

Section "Environment"
Write-Host "Folder      : $PSScriptRoot"
Write-Host "Git version : $(git --version)"
$topLevel = git rev-parse --show-toplevel 2>&1
Write-Host "Repo root   : $topLevel"

if ($topLevel -notmatch "FireProtect") {
    Write-Host ""
    Write-Host "PROBLEM: the git repo is not this folder." -ForegroundColor Red
    Write-Host "A .git folder exists somewhere above D:\My\FireProtect."
    Write-Host "Delete that stray .git, then re-run this script."
    Read-Host "`nPress Enter to close"
    exit 1
}

Section "Git identity"
$email = git config user.email
if ([string]::IsNullOrWhiteSpace($email)) {
    Write-Host "Not set - configuring now..." -ForegroundColor Yellow
    git config user.email "abhishekyadav98388@gmail.com"
    git config user.name  "Abhishek"
}
Write-Host "user.name  : $(git config user.name)"
Write-Host "user.email : $(git config user.email)"

Section "Files on disk vs files git is tracking"
# Count real project files, excluding things that should never be committed.
$onDisk = (Get-ChildItem -Recurse -File -Force |
    Where-Object {
        $_.FullName -notmatch '\\\.git\\' -and
        $_.FullName -notmatch '\\node_modules\\' -and
        $_.FullName -notmatch '\\__pycache__\\' -and
        $_.FullName -notmatch '\\\.pytest_cache\\' -and
        $_.FullName -notmatch '\\\.ruff_cache\\' -and
        $_.Extension -notin @('.db', '.db-wal', '.db-shm')
    }).Count
$tracked = (git ls-files | Measure-Object -Line).Lines
Write-Host "On disk (excluding caches) : $onDisk"
Write-Host "Tracked by git             : $tracked"

Section "Staging everything"
git add -A
$staged = (git diff --cached --name-only | Measure-Object -Line).Lines
Write-Host "Newly staged: $staged file(s)"

if ($staged -eq 0 -and $tracked -lt 50) {
    Write-Host ""
    Write-Host "PROBLEM: git staged nothing, and is tracking almost nothing." -ForegroundColor Red
    Write-Host "Checking whether an ignore rule is responsible:"
    Write-Host ""
    foreach ($probe in @("run_local.py", "Dockerfile", "backend\app\main.py", "frontend\package.json")) {
        $why = git check-ignore -v -- $probe 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  IGNORED  $probe  by rule -> $why" -ForegroundColor Red
        } else {
            Write-Host "  fine     $probe (not ignored)" -ForegroundColor Green
        }
    }
    Write-Host ""
    Write-Host "Global ignore file: $(git config --global core.excludesFile)"
    Write-Host ""
    Write-Host "Copy everything above and send it back."
    Read-Host "`nPress Enter to close"
    exit 1
}

Section "Safety check - secrets must NOT be staged"
$bad = git diff --cached --name-only | Select-String -Pattern '(^|/)\.env$|secrets\.ini$|\.db$'
if ($bad) {
    Write-Host "STOPPING: these should not be committed:" -ForegroundColor Red
    $bad | ForEach-Object { Write-Host "  $_" }
    Write-Host "Run: git reset" -ForegroundColor Yellow
    Read-Host "`nPress Enter to close"
    exit 1
}
Write-Host "Clean - no secrets or databases staged."

Section "Committing"
git commit -m "FireProtect: IoT fire detection with on-device and server-side ML"
Write-Host ""
git log --oneline -3

Section "Pushing to GitHub"
$remote = git remote get-url origin 2>&1
if ($LASTEXITCODE -ne 0) {
    git remote add origin "https://github.com/abhi964869/FireProtect.git"
    Write-Host "Remote added."
} else {
    Write-Host "Remote: $remote"
}

git branch -M main
Write-Host "Uploading ~18 MB - this can take a minute..."
git push -u origin main

Section "Result"
if ($LASTEXITCODE -eq 0) {
    Write-Host "Pushed successfully." -ForegroundColor Green
    Write-Host "Files now on GitHub: $((git ls-files | Measure-Object -Line).Lines)"
    Write-Host "https://github.com/abhi964869/FireProtect"
} else {
    Write-Host "Push failed - see the error above." -ForegroundColor Red
    Write-Host "If it mentions authentication, you need a personal access token:"
    Write-Host "  github.com -> Settings -> Developer settings ->"
    Write-Host "  Personal access tokens -> Tokens (classic) -> scope: repo"
    Write-Host "Use that token as the PASSWORD when git prompts you."
}

Read-Host "`nPress Enter to close"
