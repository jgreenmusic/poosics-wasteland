# Publish the setup tool: push the source + manifest, upload the exe to a release.
#
# Only code, docs and the manifest are committed. No game content and no
# payload archives are ever published here - TTW and Bethesda assets must not be
# redistributed, and everything else is fetched from its official home at run
# time. .gitignore enforces that.
#
# The manifest is served raw from the default branch, which is what the exe
# reads at startup. So changing the server address is: edit manifest/ttw.json,
# run this script, done - nobody reinstalls.

param(
    [string]$Repo    = 'jgreenmusic/poosics-wasteland',
    [string]$Tag     = 'v1.1.0',
    [Parameter(Mandatory)][string]$Message,   # commit message - no Co-Authored-By trailer (Julian's rule)
    [string]$Name    = 'Poosics-Wasteland-Setup',
    [switch]$SkipRelease
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$exe = "dist\$Name.exe"
if (-not $SkipRelease -and -not (Test-Path $exe)) {
    throw "$exe not found. Run .\build.ps1 first."
}

# --- repo ------------------------------------------------------------------
if (-not (Test-Path '.git')) {
    Write-Host 'Initialising the repository' -ForegroundColor Cyan
    git init -b main | Out-Null
}

git add -A
git diff --cached --quiet
$hasChanges = ($LASTEXITCODE -ne 0)
if ($hasChanges) {
    git commit -q -m $Message
    Write-Host 'Committed' -ForegroundColor Green
} else {
    Write-Host 'Nothing new to commit' -ForegroundColor DarkGray
}

# --- remote ----------------------------------------------------------------
$exists = $true
gh repo view $Repo *> $null
if ($LASTEXITCODE -ne 0) { $exists = $false }

if (-not $exists) {
    Write-Host "Creating public repo $Repo" -ForegroundColor Cyan
    gh repo create $Repo --public `
        --description "One-download setup for Tale of Two Wastelands + NV:MP co-op" `
        --source . --remote origin --push
    if ($LASTEXITCODE -ne 0) { throw 'gh repo create failed' }
} else {
    if (-not (git remote | Select-String -Quiet '^origin$')) {
        git remote add origin "https://github.com/$Repo.git"
    }
    Write-Host 'Pushing' -ForegroundColor Cyan
    git push -u origin main
    if ($LASTEXITCODE -ne 0) { throw 'git push failed' }
}

if ($SkipRelease) {
    Write-Host 'Skipping the release upload.' -ForegroundColor DarkGray
    return
}

# --- release ---------------------------------------------------------------
gh release view $Tag --repo $Repo *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Replacing the asset on existing release $Tag" -ForegroundColor Cyan
    gh release upload $Tag $exe --repo $Repo --clobber
} else {
    Write-Host "Creating release $Tag" -ForegroundColor Cyan
    gh release create $Tag $exe --repo $Repo `
        --title "Poosic's Wasteland setup $Tag" `
        --notes @'
One download, no dependencies. Run it and it sets up Tale of Two Wastelands
plus the NV:MP co-op client for you.

**You need Fallout 3 GOTY and Fallout: New Vegas with all DLC installed on
Steam, and ~45 GB free.** Setup checks all of that first and tells you exactly
what is missing rather than failing partway through.

Tale of Two Wastelands is downloaded from its own official page - it cannot be
mirrored - and setup verifies it against a pinned hash before using it. See the
README for why the load order is not optional.
'@
}
if ($LASTEXITCODE -ne 0) { throw 'release step failed' }

Write-Host ''
Write-Host 'Published:' -ForegroundColor Green
Write-Host "  https://github.com/$Repo/releases/latest"
