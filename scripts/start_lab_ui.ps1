$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

try {
    docker info --format '{{.ServerVersion}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker Engine is unavailable.' }
} catch {
    throw '请先启动 Docker Desktop，然后重新运行 start-ui.cmd。'
}

docker compose up -d
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose 启动失败。' }

function Test-Http([string]$uri) {
    try {
        $response = Invoke-WebRequest -Uri $uri -TimeoutSec 3 -UseBasicParsing
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Test-Console {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:18010/' -TimeoutSec 3 -UseBasicParsing
        return $response.StatusCode -eq 200 -and $response.Content.Contains('AegisOps')
    } catch {
        return $false
    }
}

$apiReady = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $controlReady = Test-Http 'http://127.0.0.1:18000/api/v1/health/ready'
    $catalogReady = Test-Http 'http://127.0.0.1:8004/catalog'
    if ($controlReady -and $catalogReady) {
        $apiReady = $true
        break
    }
    if ($attempt -eq 0) { Write-Output "等待服务就绪：control=$controlReady catalog=$catalogReady" }
    Start-Sleep -Seconds 1
}
if (-not $apiReady) { throw '业务或控制面仍未就绪，请检查 docker compose ps。' }

$consoleUrl = 'http://127.0.0.1:18010/?view=ops'
if (-not (Test-Console)) {
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { (Get-Command python -ErrorAction Stop).Source }
    Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'apps.lab_console.main:app', '--host', '127.0.0.1', '--port', '18010') -WorkingDirectory $projectRoot -WindowStyle Hidden | Out-Null
    $consoleReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-Console) {
            $consoleReady = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $consoleReady) { throw '事故工作台启动失败，请手动运行 python -m uvicorn apps.lab_console.main:app --host 127.0.0.1 --port 18010。' }
}

Write-Output "AegisOps 事故工作台：$consoleUrl"
Write-Output '业务商城：http://127.0.0.1:18010/'
Start-Process -FilePath $consoleUrl | Out-Null
