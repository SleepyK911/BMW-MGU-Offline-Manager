$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot '.build/runtime'
New-Item -ItemType Directory -Force $runtimeDir | Out-Null
$nodeBinary = Join-Path $runtimeDir 'node.exe'
if (-not (Test-Path -LiteralPath $nodeBinary)) {
    Invoke-WebRequest 'https://nodejs.org/dist/v26.13.1/win-x64/node.exe' -OutFile $nodeBinary
}
if ((Get-FileHash -LiteralPath $nodeBinary -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'f5109dc868ec38703ad8f305e0aceece717cb60d4889873f434017af4e96cccb') {
    throw 'Node.js SHA-256 mismatch'
}
$installerScript = Join-Path $projectRoot 'vendor/mgu-install.js'
if ((Get-FileHash -LiteralPath $installerScript -Algorithm SHA256).Hash.ToLowerInvariant() -ne '338c9f1d6857162a64361ed69541d234eef0e189c3d29d0c57e2c32ef89cd456') {
    throw 'MGU installer SHA-256 mismatch'
}
Push-Location $projectRoot
try {
    python -m PyInstaller --noconfirm --windowed --onedir --name BMW_MGU_Offline_Manager --add-data "$nodeBinary;." --add-data "$installerScript;." app/manager.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
    $outputDir = Join-Path $projectRoot 'dist/BMW_MGU_Offline_Manager'
    Copy-Item -LiteralPath (Join-Path $projectRoot 'app') -Destination (Join-Path $outputDir 'source') -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'licenses') -Destination (Join-Path $outputDir 'source/licenses') -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination (Join-Path $outputDir 'LICENSE') -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY_NOTICES.md') -Destination $outputDir -Force
    New-Item -ItemType Directory -Force (Join-Path $outputDir 'USB') | Out-Null
    Write-Output "Built $outputDir"
} finally {
    Pop-Location
}
