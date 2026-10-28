$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Nie znaleziono interpretera projektu. Utwórz venv i zainstaluj requirements-build.txt."
}

Push-Location $projectRoot
try {
    & $pythonPath -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --noupx `
        --name MyChat `
        --collect-all ttkbootstrap `
        --collect-all pygments `
        --collect-all PIL `
        --collect-all openai `
        --distpath (Join-Path $projectRoot "dist") `
        --workpath (Join-Path $projectRoot "build") `
        --specpath (Join-Path $projectRoot "build") `
        (Join-Path $projectRoot "main.py")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller zakończył się kodem $LASTEXITCODE."
    }
    Write-Host "Gotowe: $(Join-Path $projectRoot 'dist\MyChat.exe')"
}
finally {
    Pop-Location
}
