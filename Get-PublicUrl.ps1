param(
    [switch]$Open
)

$logs = docker compose --profile demo logs --no-color --tail=100 tunnel 2>$null
$matches = [regex]::Matches(($logs -join "`n"), 'https://[a-z0-9-]+\.trycloudflare\.com')

if ($matches.Count -eq 0) {
    Write-Error 'No se encontró una URL pública. Arranca primero el túnel con: docker compose --profile demo up -d'
    exit 1
}

$url = $matches[$matches.Count - 1].Value
Write-Output $url

if ($Open) {
    Start-Process $url
}
