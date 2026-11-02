@echo off
setlocal
if not exist "%~dp0logs" mkdir "%~dp0logs"
set DOUBAO_DESKTOP_ROOT=%~dp0.
start "" "%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -WindowStyle Hidden -Command ^
 "$ErrorActionPreference='Stop'; $root=[IO.Path]::GetFullPath($env:DOUBAO_DESKTOP_ROOT).TrimEnd('\'); $temp=Join-Path $root '.tmp'; [IO.Directory]::CreateDirectory($temp)|Out-Null;" ^
 "$hash=[Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($root)); $name='Local\DoubaoDesktopBridge_'+([BitConverter]::ToString($hash)).Replace('-','').Substring(0,16); $mutex=New-Object Threading.Mutex($false,$name); try{$owns=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$owns=$true}; if(-not $owns){exit};" ^
 "$encoding=New-Object Text.UTF8Encoding($false); function Save($path,$value){$tmp=$path+'.tmp'; [IO.File]::WriteAllText($tmp,($value|ConvertTo-Json -Compress),$encoding); Move-Item -LiteralPath $tmp -Destination $path -Force};" ^
 "$folder=Join-Path $root 'workspace\videos'; $requestFile=Join-Path $temp 'desktop_folder_request.json'; $responseFile=Join-Path $temp 'desktop_folder_response.json'; $statusFile=Join-Path $temp 'desktop_bridge_status.json'; $last=''; $beat=[DateTime]::MinValue;" ^
 "try{while($true){ if(([DateTime]::UtcNow-$beat).TotalSeconds -ge 1){Save $statusFile @{pid=$PID;ready=$true}; $beat=[DateTime]::UtcNow};" ^
 "if(Test-Path -LiteralPath $requestFile){try{$q=[IO.File]::ReadAllText($requestFile)|ConvertFrom-Json}catch{$q=$null}; if($q -and $q.id -and $q.id -ne $last){$last=[string]$q.id; if(([DateTime]::UtcNow-(Get-Item -LiteralPath $requestFile).LastWriteTimeUtc).TotalSeconds -lt 15){try{" ^
 "$shell=New-Object -ComObject Shell.Application; $shell.Explore($folder); $ok=$false; $deadline=[DateTime]::UtcNow.AddSeconds(5); do{foreach($w in @($shell.Windows())){try{$u=[uri]$w.LocationURL; if($u.IsFile -and [string]::Equals($u.LocalPath,$folder,[StringComparison]::OrdinalIgnoreCase)){$ok=$true;break}}catch{}}; if(-not $ok){Start-Sleep -Milliseconds 100}}while(-not $ok -and [DateTime]::UtcNow -lt $deadline); if(-not $ok){throw 'Video folder window was not confirmed'}; Save $responseFile @{id=$last;ok=$true;folder=$folder}}" ^
 "catch{Save $responseFile @{id=$last;ok=$false;error=$_.Exception.Message}}}}}; Start-Sleep -Milliseconds 150}}finally{$mutex.ReleaseMutex();$mutex.Dispose()}" >>"%~dp0logs\desktop_bridge.log" 2>&1
exit /b 0
