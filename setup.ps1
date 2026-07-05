# Setup completo do projeto Spreading (Windows / PowerShell)
# Uso:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Faz: deps Python + navegador Playwright + deps Node + modelo do Ollama.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "==> 1/4 Dependencias Python (requirements.txt)" -ForegroundColor Cyan
pip install -r "$root\python\requirements.txt"

Write-Host "==> 2/4 Navegador do Playwright (Chromium)" -ForegroundColor Cyan
python -m playwright install chromium

Write-Host "==> 3/4 Dependencias do servico Node (WhatsApp)" -ForegroundColor Cyan
Push-Location "$root\node.js"
npm install
Pop-Location

Write-Host "==> 4/4 Modelo do Ollama (LLM local)" -ForegroundColor Cyan
# ollama pode nao estar no PATH desta sessao; tenta o caminho padrao de instalacao.
$ollama = "ollama"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    $cand = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $cand) { $ollama = $cand }
    else { Write-Warning "Ollama nao encontrado no PATH. Instale em https://ollama.com/download"; $ollama = $null }
}
if ($ollama) {
    $modelo = if ($env:OLLAMA_MODEL) { $env:OLLAMA_MODEL } else { "qwen2.5:3b" }
    Write-Host "    baixando modelo $modelo ..."
    & $ollama pull $modelo
}

Write-Host "`nPronto! Suba os servicos:" -ForegroundColor Green
Write-Host "  - WhatsApp:  cd node.js; node index.js"
Write-Host "  - Django:    cd python\django; python manage.py runserver"
