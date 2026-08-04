# Lê ../.env.fly e envia os segredos p/ os apps Fly.
#   spreading-wa  <- somente WA_CAPABILITY_PUBLIC_KEYS_JSON
#   spreading-web <- signer privado, keyring ML, URLs PostgreSQL e integrações
# Placeholders (<...>) e vazios são ignorados. Rode da raiz do repo:
#   powershell -ExecutionPolicy Bypass -File deploy\push-secrets.ps1
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repo '.env.fly'
if (-not (Test-Path $envFile)) { throw ".env.fly nao encontrado em $envFile" }

$vals = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*#') { continue }
    if ($line -notmatch '=') { continue }
    $idx = $line.IndexOf('=')
    $k = $line.Substring(0, $idx).Trim()
    $v = $line.Substring($idx + 1).Trim()
    if ($k -eq '' -or $v -eq '') { continue }
    if ($v -like '<*>') { continue }   # placeholder nao preenchido
    $vals[$k] = $v
}

$waKeys = @('WA_CAPABILITY_PUBLIC_KEYS_JSON', 'WA_CAPABILITY_PUBLIC_KEYS_JSON_B64')
$waArgs = @()
foreach ($k in $waKeys) {
    if ($vals.ContainsKey($k)) { $waArgs += ($k + '=' + $vals[$k]) }
}
if ($waArgs.Count -gt 0) {
    Write-Host ("-> " + $waArgs.Count + " chave(s) publica(s) em spreading-wa")
    & fly secrets set @waArgs --app spreading-wa
    if ($LASTEXITCODE -ne 0) { throw "falha ao setar chaves publicas (spreading-wa)" }
}

$webArgs = @()
foreach ($k in $vals.Keys) {
    if ($k -eq 'API_KEY' -or $k -eq 'WHATSAPP_API_KEY') { continue }
    if ($waKeys -contains $k) { continue }
    $webArgs += ($k + '=' + $vals[$k])
}

if ($webArgs.Count -gt 0) {
    Write-Host ("-> " + $webArgs.Count + " segredos em spreading-web")
    & fly secrets set @webArgs --app spreading-web
    if ($LASTEXITCODE -ne 0) { throw "falha ao setar segredos (spreading-web)" }
}
Write-Host "OK."
