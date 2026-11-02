# Optional manual verification. This script is not run by the project launcher.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw '未找到发布副本的 .venv\Scripts\python.exe；请先安装项目依赖。'
}
$tempParent = Join-Path $env:LOCALAPPDATA 'Temp'
if (-not (Test-Path -LiteralPath $tempParent -PathType Container)) {
    throw 'LOCALAPPDATA 下的 Temp 目录不存在。'
}
$name = 'doubao-publish-verify-' + [guid]::NewGuid().ToString('N')
$target = Join-Path $tempParent $name
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$oldTestDir = $env:DOUBAO_TEST_TEMP_DIR
$oldBytecode = $env:PYTHONDONTWRITEBYTECODE
$created = $false
try {
    New-Item -ItemType Directory -Path $target -ErrorAction Stop | Out-Null
    $created = $true
    $env:TEMP = $target
    $env:TMP = $target
    $env:DOUBAO_TEST_TEMP_DIR = $target
    $env:PYTHONDONTWRITEBYTECODE = '1'
    # A single mkdir attempt fails promptly instead of tempfile retrying indefinitely.
    $probe = "import os; p=os.path.join(os.environ['DOUBAO_TEST_TEMP_DIR'],'probe'); os.mkdir(p); os.rmdir(p)"
    & $python -B -c $probe
    if ($LASTEXITCODE -ne 0) { throw 'Python 无法在独立测试目录写入；停止测试。' }
    Push-Location -LiteralPath $root
    try {
        & $python -B -m unittest discover -s tests -p test_dashboard_security.py -k test_authorized_save_still_operates -v
        $save = $LASTEXITCODE
        if ($save -ne 0) {
            Write-Host '保存测试：FAIL；删除测试：未运行（已停止）'
            exit 1
        }
        & $python -B -m unittest discover -s tests -p test_dashboard_security.py -k test_authorized_delete_only_removes_disposable_fixture -v
        $delete = $LASTEXITCODE
        if ($save -eq 0) { Write-Host '保存测试：PASS' } else { Write-Host '保存测试：FAIL' }
        if ($delete -eq 0) { Write-Host '删除测试：PASS' } else { Write-Host '删除测试：FAIL' }
        if ($delete -ne 0) { exit 1 }
    }
    finally { Pop-Location }
}
finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    $env:DOUBAO_TEST_TEMP_DIR = $oldTestDir
    $env:PYTHONDONTWRITEBYTECODE = $oldBytecode
    if ($created -and (Test-Path -LiteralPath $target)) {
        try {
            $item = Get-Item -LiteralPath $target -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '目录已变为重解析点' }
            # Delete only the exact, now-empty directory this invocation created.
            [IO.Directory]::Delete($target, $false)
        }
        catch { Write-Warning ('测试目录未清理，请检查：' + $target) }
    }
}
