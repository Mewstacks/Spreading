# Lê ../.env.fly e envia os segredos p/ os apps Fly.
#   spreading-wa  <- API_KEY
#   spreading-web <- todo o resto + WHATSAPP_API_KEY (= API_KEY)
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

if ($vals.ContainsKey('API_KEY')) {
    Write-Host "-> API_KEY em spreading-wa"
    & fly secrets set ("API_KEY=" + $vals['API_KEY']) --app spreading-wa
    if ($LASTEXITCODE -ne 0) { throw "falha ao setar API_KEY (spreading-wa)" }
}

$webArgs = @()
foreach ($k in $vals.Keys) {
    if ($k -eq 'API_KEY') { continue }
    $webArgs += ($k + '=' + $vals[$k])
}
if ($vals.ContainsKey('API_KEY')) { $webArgs += ('WHATSAPP_API_KEY=' + $vals['API_KEY']) }

if ($webArgs.Count -gt 0) {
    Write-Host ("-> " + $webArgs.Count + " segredos em spreading-web")
    & fly secrets set @webArgs --app spreading-web
    if ($LASTEXITCODE -ne 0) { throw "falha ao setar segredos (spreading-web)" }
}
Write-Host "OK."
