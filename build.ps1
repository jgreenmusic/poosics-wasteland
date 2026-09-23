# Build the single-file friend-facing installer.
#
# One .exe, no console: the player double-clicks it and gets a window. There is
# deliberately no Python, no 7-Zip and no VC++ prerequisite on their side -
# py7zr is pure Python and Tkinter ships with the interpreter, so PyInstaller
# can fold everything into the one file.
#
# The manifest is bundled ONLY as an offline fallback. At run time the exe
# prefers the published copy on GitHub, so changing the server IP means
# re-publishing one small JSON file rather than rebuilding and redistributing.

param(
    [string]$Name = 'Poosics-Wasteland-Setup',
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

if ($Clean) {
    foreach ($d in 'build', 'dist', "$Name.spec") {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force }
    }
}

# 7-Zip is bundled because py7zr cannot decode the BCJ2 filter that the TTW and
# TTW OGG archives use (it fails partway and leaves zero-byte files). 7-Zip is
# LGPL; its License.txt ships next to it inside the exe.
$sevenZip = Join-Path $env:ProgramFiles '7-Zip'
foreach ($f in '7z.exe', '7z.dll', 'License.txt') {
    if (-not (Test-Path (Join-Path $sevenZip $f))) { throw "7-Zip file missing: $sevenZip\$f - install 7-Zip x64 first" }
}

Write-Host 'Building ' -NoNewline
Write-Host $Name -ForegroundColor Cyan

python -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name $Name `
    --paths 'src' `
    --add-data 'manifest/ttw.json;manifest' `
    --add-binary "$sevenZip\7z.exe;tools" `
    --add-binary "$sevenZip\7z.dll;tools" `
    --add-data "$sevenZip\License.txt;tools" `
    --hidden-import 'py7zr' `
    --collect-submodules 'py7zr' `
    'src/dojo_setup.py'

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$exe = Join-Path $root "dist\$Name.exe"
if (-not (Test-Path $exe)) { throw "Build reported success but $exe is missing" }

$size = (Get-Item $exe).Length
Write-Host ''
Write-Host 'Built ' -NoNewline
Write-Host $exe -ForegroundColor Green
Write-Host ("  {0:N1} MB" -f ($size / 1MB))
Write-Host ''
Write-Host 'Publish with:  .\publish.ps1' -ForegroundColor DarkGray
