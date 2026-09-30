param([string]$ResultPath)

$ErrorActionPreference = 'Stop'
$launchRoot = Split-Path -Parent $PSScriptRoot
$transcript = Join-Path ([IO.Path]::GetTempPath()) ("maabangdream-launch-{0}.log" -f [Guid]::NewGuid().ToString('N'))
$exitCode = 0
Start-Transcript -LiteralPath $transcript -Force | Out-Null
try {
    # 不经 CMD；改名后重新生成便携路径，仍执行运行库准备和版本检查。
    Set-Location -LiteralPath (Split-Path -Parent $launchRoot)
    $launchRoot = & (Join-Path $launchRoot 'scripts\normalize-release-directory.ps1') -Inline
    & (Join-Path $launchRoot 'scripts\start-release.ps1')
    if ($LASTEXITCODE -ne 0) { throw "Portable launch failed: $LASTEXITCODE" }
} catch {
    Write-Warning $_.Exception.Message
    $exitCode = 1
} finally {
    Stop-Transcript | Out-Null
    if ($ResultPath) {
        try {
            # 回传改名后的真实目录，避免更新器收尾保存日志时重建旧版本目录。
            $result = @{ install_root = $launchRoot } | ConvertTo-Json -Compress
            [IO.File]::WriteAllText($ResultPath, $result, [Text.UTF8Encoding]::new($false))
        } catch {
            Write-Warning "Unable to save restart result: $($_.Exception.Message)"
        }
    }
    try {
        $logs = Join-Path $launchRoot 'logs'
        New-Item -ItemType Directory -Path $logs -Force | Out-Null
        Copy-Item -LiteralPath $transcript -Destination (Join-Path $logs 'updater-launch.log') -Force
        Remove-Item -LiteralPath $transcript -Force
    } catch {
        # 日志保留在临时目录，不让日志写入错误覆盖原始启动结果。
        Write-Warning "Launch transcript retained at $transcript"
    }
}
exit $exitCode
