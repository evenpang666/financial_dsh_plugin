param([string]$FixtureRoot, [string]$ActualNode, [switch]$CorruptChecksum)
$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
New-Item -ItemType Directory -Force (Join-Path $FixtureRoot 'scripts') | Out-Null
Copy-Item -LiteralPath (Join-Path $repository 'scripts/bootstrap-node.ps1') -Destination (Join-Path $FixtureRoot 'scripts/bootstrap-node.ps1')
Copy-Item -LiteralPath (Join-Path $repository 'scripts/runtime-versions.json') -Destination (Join-Path $FixtureRoot 'scripts/runtime-versions.json')
Set-Content -Encoding Ascii -LiteralPath (Join-Path $FixtureRoot 'scripts/launch.mjs') -Value 'import {writeFileSync} from "node:fs"; writeFileSync("result.json",JSON.stringify({node:process.execPath,args:process.argv.slice(2),npm:process.env.npm_execpath}));'
. (Join-Path $repository 'scripts/bootstrap-node.ps1')
foreach ($version in @('20.0.0', '22.12.9', '22.13.0', '24.21.0')) {
    $fake = Join-Path $FixtureRoot "node-$version.cmd"
    Set-Content -Encoding Ascii -LiteralPath $fake -Value "@echo off`r`necho v$version"
    if ((Test-Node $fake) -ne ([version]$version -ge [version]'22.13.0')) { throw "Incorrect Node version check: $version" }
}
if (Test-Node (Join-Path $FixtureRoot 'missing.exe')) { throw 'Missing Node accepted.' }
$null = Get-FileHash -LiteralPath $fake -Algorithm SHA256

# Simulate the absent-Node download branch without network or changing system tools.
$global:financeFixturedownloadCount = 0
$global:financeFixturearchiveVersion = (Get-Content (Join-Path $repository 'scripts/runtime-versions.json') -Raw | ConvertFrom-Json).node
$global:financeFixturearchitecture = if ("$env:PROCESSOR_ARCHITECTURE $env:PROCESSOR_ARCHITEW6432" -match 'ARM64') { 'arm64' } else { 'x64' }
$global:financeFixturearchiveName = "node-v$global:financeFixturearchiveVersion-win-$global:financeFixturearchitecture.zip"
$global:financeFixturefixtureBytes = [Text.Encoding]::ASCII.GetBytes('verified archive fixture')
$global:financeFixturechecksum = ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($global:financeFixturefixtureBytes))).Replace('-','').ToLower()
if ($CorruptChecksum) {
    $global:financeFixturechecksum = '0' * 64
    New-Item -ItemType Directory -Force (Join-Path $FixtureRoot '.runtime/node/current') | Out-Null
    Set-Content -Encoding Ascii -LiteralPath (Join-Path $FixtureRoot '.runtime/node/current/retained.txt') -Value 'old runtime retained'
}
function Get-Command($Name, $ErrorAction) { if ($Name -in @('node.exe','npm.cmd')) { return $null }; Microsoft.PowerShell.Core\Get-Command $Name }
function Invoke-WebRequest([switch]$UseBasicParsing, $Uri, $OutFile) {
    $global:financeFixturedownloadCount++
    if ($Uri -like '*SHASUMS256.txt') { Set-Content -Encoding Ascii -LiteralPath $OutFile -Value "$global:financeFixturechecksum  $global:financeFixturearchiveName" }
    else { [IO.File]::WriteAllBytes($OutFile,$global:financeFixturefixtureBytes) }
}
function Expand-Archive($Path, $DestinationPath) {
    $unpacked = Join-Path $DestinationPath ($global:financeFixturearchiveName -replace '\.zip$', '')
    New-Item -ItemType Directory -Force (Join-Path $unpacked 'node_modules/npm/bin') | Out-Null
    Copy-Item -LiteralPath $ActualNode -Destination (Join-Path $unpacked 'node.exe')
    Set-Content -Encoding Ascii -LiteralPath (Join-Path $unpacked 'node_modules/npm/bin/npm-cli.js') -Value 'console.log("10.9.2")'
}
& (Join-Path $FixtureRoot 'scripts/bootstrap-node.ps1') --no-open
if ($CorruptChecksum) {
    if ($LASTEXITCODE -eq 0) { throw 'Corrupt download was accepted.' }
    exit 0
}
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap fixture failed.' }
if ($global:financeFixturedownloadCount -ne 2) { throw 'Expected archive and checksum downloads.' }
