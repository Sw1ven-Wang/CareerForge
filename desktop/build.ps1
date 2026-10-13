param([switch]$SkipBuilder)
$ErrorActionPreference = 'Stop'
$taskProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (!(Test-Path -LiteralPath (Join-Path $taskProjectRoot 'compose.ide.yaml'))) { throw 'CareerForge project not found.' }
if (!$SkipBuilder) {
    & docker build -t careerforge-desktop-builder:local -f (Join-Path $PSScriptRoot 'build/Dockerfile') (Join-Path $PSScriptRoot 'build')
    if ($LASTEXITCODE -ne 0) { throw 'Desktop builder preparation failed.' }
}
$taskDesktopDir = (Resolve-Path $PSScriptRoot).Path
& docker run --rm --mount "type=bind,source=$taskDesktopDir,target=/project/desktop" --mount 'type=volume,source=careerforge_desktop_registry,target=/root/.cargo/registry' --mount 'type=volume,source=careerforge_desktop_sdk,target=/cache/xwin' careerforge-desktop-builder:local cargo xwin build --target x86_64-pc-windows-msvc --release --locked
if ($LASTEXITCODE -ne 0) { throw 'Desktop compilation failed.' }
$taskReleaseDir = Join-Path $PSScriptRoot 'release'
New-Item -ItemType Directory -Force $taskReleaseDir | Out-Null
$taskVersion = (Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'src-tauri/tauri.conf.json') | ConvertFrom-Json).version
$taskExecutable = Join-Path $taskReleaseDir ('CareerForge-' + $taskVersion + '.exe')
if (Get-Process | Where-Object { $_.Path -eq $taskExecutable }) { throw 'This version is running. Close its desktop windows before publishing; backend services stay running.' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'src-tauri/target/x86_64-pc-windows-msvc/release/careerforge-desktop.exe') -Destination $taskExecutable -Force
$taskShell = New-Object -ComObject WScript.Shell
$taskShortcut = $taskShell.CreateShortcut((Join-Path $taskProjectRoot 'CareerForge.lnk'))
$taskShortcut.TargetPath = $taskExecutable
$taskShortcut.WorkingDirectory = $taskProjectRoot
$taskShortcut.Description = 'CareerForge desktop workbench'
$taskShortcut.Save()
Write-Output "Desktop ready: $taskExecutable"
