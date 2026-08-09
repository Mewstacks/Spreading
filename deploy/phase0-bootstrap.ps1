param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('production')]
    [string]$Environment
)

$ErrorActionPreference = 'Stop'

# O ambiente de homologação foi desativado em 09/08/2026 para cortar custo: os apps
# spreading-web-staging, spreading-wa-staging e spreading-staging-db foram destruídos
# no Fly junto com seus volumes. Só existe 'production'.
$webApp = 'spreading-web'
$waApp = 'spreading-wa'
$dbHost = 'spreading-db.flycast'
$dbName = 'spreading_web'

function Assert-LastExit([string]$message) {
    if ($LASTEXITCODE -ne 0) {
        throw $message
    }
}

function New-UrlSafeSecret([int]$byteCount = 36) {
    $bytes = [byte[]]::new($byteCount)
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Import-FlySecrets([string]$app, [hashtable]$values) {
    [string[]]$secretArgs = @(foreach ($key in ($values.Keys | Sort-Object)) {
        "$key=$($values[$key])"
    })
    & fly secrets set $secretArgs --app $app
    Assert-LastExit "Falha ao importar secrets em $app."
}

function ConvertTo-UrlBase64([string]$value) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($value)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

# É um bootstrap destrutivo se repetido: uma KEK nova tornaria sessões já
# cifradas indecifráveis. Recusamos automaticamente um ambiente já inicializado.
$existing = & fly secrets list --app $webApp --json | ConvertFrom-Json
Assert-LastExit "Não foi possível inspecionar os secrets de $webApp."
if ($existing.name -contains 'ML_SESSION_KEKS_JSON_B64' -or
    $existing.name -contains 'SYSTEM_DATABASE_URL' -or
    $existing.name -contains 'MIGRATION_DATABASE_URL') {
    throw "O bootstrap da Fase 0 já foi executado em $webApp; nenhuma chave foi alterada."
}

$runtimePassword = New-UrlSafeSecret
$systemPassword = New-UrlSafeSecret
$migrationPassword = New-UrlSafeSecret
$tenantContextKey = New-UrlSafeSecret 48

$cryptoScript = @'
import base64
import json
import os
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

private = Ed25519PrivateKey.generate()
print(json.dumps({
    "ml_kek": b64(os.urandom(32)),
    "wa_private": b64(private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )),
    "wa_public": b64(private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )),
}, separators=(",", ":")))
'@
$crypto = ($cryptoScript | & python - | ConvertFrom-Json)
Assert-LastExit 'Falha ao gerar o material criptográfico da Fase 0.'

$bootstrap = @{
    runtime = $runtimePassword
    system = $systemPassword
    migration = $migrationPassword
} | ConvertTo-Json -Compress
Import-FlySecrets $webApp @{
    PHASE0_DB_BOOTSTRAP_B64 = ConvertTo-UrlBase64 $bootstrap
}

# O código remoto lê as senhas somente do secret temporário. Ele não mostra
# valores e não usa REASSIGN OWNED: altera apenas objetos da aplicação em public.
$repo = Split-Path -Parent $PSScriptRoot
$remoteBootstrap = Join-Path $PSScriptRoot 'phase0-remote-bootstrap.py'
& fly ssh sftp put $remoteBootstrap /tmp/phase0-remote-bootstrap.py --app $webApp
Assert-LastExit "Falha ao enviar o bootstrap temporário para $webApp."
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$sshOutput = & fly ssh console --app $webApp --command "python /tmp/phase0-remote-bootstrap.py" 2>&1
$ErrorActionPreference = $previousErrorAction
$sshOutput | Out-Host
if (($sshOutput -join "`n") -notmatch 'Roles tenant provisionadas e verificadas') {
    throw "Falha ao provisionar as roles PostgreSQL em $Environment."
}

function New-DatabaseUrl([string]$role, [string]$password) {
    $encodedPassword = [Uri]::EscapeDataString($password)
    return "postgresql://${role}:${encodedPassword}@${dbHost}:5432/${dbName}?sslmode=disable"
}

$keyId = 'wa-ed25519-v1'
$mlKeyring = @{ v1 = $crypto.ml_kek } | ConvertTo-Json -Compress
$waPublicKeys = @{ $keyId = $crypto.wa_public } | ConvertTo-Json -Compress

Import-FlySecrets $webApp @{
    DATABASE_URL = New-DatabaseUrl 'spreading_runtime' $runtimePassword
    SYSTEM_DATABASE_URL = New-DatabaseUrl 'spreading_system' $systemPassword
    MIGRATION_DATABASE_URL = New-DatabaseUrl 'spreading_migration' $migrationPassword
    TENANT_CONTEXT_SIGNING_KEY = $tenantContextKey
    PHASE0_EXPAND_ONLY = '1'
    ML_SESSION_KEKS_JSON_B64 = ConvertTo-UrlBase64 $mlKeyring
    ML_SESSION_CURRENT_KEY_VERSION = 'v1'
    WA_CAPABILITY_PRIVATE_KEY = $crypto.wa_private
    WA_CAPABILITY_KEY_ID = $keyId
}
Import-FlySecrets $waApp @{
    WA_CAPABILITY_PUBLIC_KEYS_JSON_B64 = ConvertTo-UrlBase64 $waPublicKeys
}

& fly secrets unset PHASE0_DB_BOOTSTRAP_JSON PHASE0_DB_BOOTSTRAP_B64 --app $webApp
Assert-LastExit "Falha ao remover o secret temporário de bootstrap."

Write-Host "Bootstrap $Environment concluído. Valores secretos não foram exibidos nem gravados em arquivo."
