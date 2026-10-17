$ErrorActionPreference = 'Stop'
# A parent PowerShell 7 process may put incompatible modules before Windows PowerShell's.
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator + $env:PSModulePath
$projectRoot = Split-Path $PSScriptRoot -Parent
$managedRoot = Join-Path $projectRoot '.runtime\node'
$currentRoot = Join-Path $managedRoot 'current'
$minimum = (Get-Content (Join-Path $PSScriptRoot 'runtime-versions.json') -Raw | ConvertFrom-Json).nodeMinimum

function Test-Node($path) {
    if (-not $path -or -not (Test-Path $path)) { return $false }
    try {
        $version = & $path --version 2>$null
        return $LASTEXITCODE -eq 0 -and [version]($version.TrimStart('v')) -ge [version]$minimum
    } catch { return $false }
}

function Test-Npm($nodePath, $cliPath) {
    try { & $nodePath $cliPath --version *> $null; return $LASTEXITCODE -eq 0 } catch { return $false }
}

function Assert-RuntimePath($path) {
    $checked = [IO.Path]::GetFullPath($path)
    if (-not $checked.StartsWith([IO.Path]::GetFullPath($managedRoot) + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid runtime directory.' }
    return $checked
}

if ($MyInvocation.InvocationName -eq '.') { return }
try {
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    $nodePath = if ($nodeCommand) { $nodeCommand.Source } else { $null }
    $systemNodePath = $nodePath
    $systemReady = (Test-Node $nodePath) -and $null -ne $npmCommand
    if ($systemReady) {
        try { & $npmCommand.Source --version *> $null; $systemReady = $LASTEXITCODE -eq 0 } catch { $systemReady = $false }
    }
    if (-not $systemReady) {
        $nodePath = Join-Path $currentRoot 'node.exe'
        $npmCli = Join-Path $currentRoot 'node_modules\npm\bin\npm-cli.js'
        if (-not ((Test-Node $nodePath) -and (Test-Npm $nodePath $npmCli))) {
            $processor = "$env:PROCESSOR_ARCHITECTURE $env:PROCESSOR_ARCHITEW6432"
            if ($processor -match 'ARM64') { $architecture = 'arm64' }
            elseif ($processor -match 'AMD64') { $architecture = 'x64' }
            else { throw 'Automatic Node.js setup supports 64-bit Windows (x64 or arm64).' }
            New-Item -ItemType Directory -Force -Path $managedRoot | Out-Null
            $staging = Join-Path $managedRoot ('.install-' + [guid]::NewGuid())
            New-Item -ItemType Directory -Path $staging | Out-Null
            try {
                $version = (Get-Content (Join-Path $PSScriptRoot 'runtime-versions.json') -Raw | ConvertFrom-Json).node
                $release = "v$version"
                Write-Host "[Environment] Installing project-local Node.js $version and npm..."
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                $sumsPath = Join-Path $staging 'SHASUMS256.txt'
                Invoke-WebRequest -UseBasicParsing -Uri "https://nodejs.org/dist/$release/SHASUMS256.txt" -OutFile $sumsPath
                $match = Get-Content $sumsPath | Select-String -Pattern "^([a-fA-F0-9]{64})\s+(node-($([regex]::Escape($release)))-win-$architecture\.zip)$" | Select-Object -First 1
                if (-not $match) { throw 'The official Node.js archive was not found for this platform.' }
                $expected = $match.Matches[0].Groups[1].Value
                $archiveName = $match.Matches[0].Groups[2].Value
                $release = $match.Matches[0].Groups[3].Value
                $archive = Join-Path $staging $archiveName
                Invoke-WebRequest -UseBasicParsing -Uri "https://nodejs.org/dist/$release/$archiveName" -OutFile $archive
                if ((Get-FileHash -Algorithm SHA256 $archive).Hash -ne $expected) { throw 'Node.js download checksum mismatch. Start again to retry.' }
                Expand-Archive -Path $archive -DestinationPath $staging
                $unpacked = Join-Path $staging ($archiveName -replace '\.zip$', '')
                $newNode = Join-Path $unpacked 'node.exe'
                if (-not ((Test-Node $newNode) -and (Test-Npm $newNode (Join-Path $unpacked 'node_modules\npm\bin\npm-cli.js')))) { throw 'Downloaded Node.js or npm cannot run.' }
                $previous = Join-Path $staging 'previous'
                $currentRoot = Assert-RuntimePath $currentRoot
                $previous = Assert-RuntimePath $previous
                $unpacked = Assert-RuntimePath $unpacked
                if (Test-Path $currentRoot) { Move-Item -LiteralPath $currentRoot -Destination $previous }
                try { Move-Item -LiteralPath $unpacked -Destination $currentRoot }
                catch { if (Test-Path $previous) { Move-Item -LiteralPath $previous -Destination $currentRoot }; throw }
            } finally {
                $checked = [IO.Path]::GetFullPath($staging)
                if (-not $checked.StartsWith([IO.Path]::GetFullPath($managedRoot) + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid runtime staging directory.' }
                Remove-Item -LiteralPath $checked -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        $env:npm_execpath = $npmCli
        $env:PATH = $currentRoot + [IO.Path]::PathSeparator + $env:PATH
        if (Test-Node $systemNodePath) { $nodePath = $systemNodePath }
    }
    $env:PATH = (Split-Path $nodePath -Parent) + [IO.Path]::PathSeparator + $env:PATH
    $env:FINANCE_BOOTSTRAPPED = '1'
    $launchArguments = @($args)
    if ($env:FINANCE_BOOTSTRAP_ARGS) { $launchArguments = @(ConvertFrom-Json $env:FINANCE_BOOTSTRAP_ARGS); Remove-Item Env:FINANCE_BOOTSTRAP_ARGS }
    Set-Location $projectRoot
    & $nodePath (Join-Path $projectRoot 'scripts\launch.mjs') @launchArguments
    exit $LASTEXITCODE
} catch {
    Write-Host ('Automatic environment setup failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
