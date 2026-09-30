# md2pdf.ps1 - Markdown to PDF for reMarkable tablets and paper.
# Finds Python 3.13+ (or installs a private copy from python.org), then runs md2pdf.py with the same arguments.
#   .\md2pdf.ps1 notes.md        .\md2pdf.ps1 --help        .\md2pdf.ps1 --add-to-path
# Works in Windows PowerShell 5.1 and PowerShell 7. There is deliberately no param() block, so every argument
# reaches md2pdf.py untouched.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$ScriptArgs = @($args)
$ToolDir = $PSScriptRoot
$Engine = Join-Path $ToolDir 'md2pdf.py'
$MinPython = [version]'3.13'

function Write-Note([string]$Text) { [Console]::Error.WriteLine("md2pdf: $Text") }

function Get-RuntimeHome {
    # MD2PDF_HOME if set, else .runtime next to this script, else %LOCALAPPDATA%\md2pdf. It must be writable.
    $dirs = @()
    if ($env:MD2PDF_HOME) { $dirs += $env:MD2PDF_HOME }
    else {
        $dirs += (Join-Path $ToolDir '.runtime')
        if ($env:LOCALAPPDATA) { $dirs += (Join-Path $env:LOCALAPPDATA 'md2pdf') }
    }
    foreach ($dir in $dirs) {
        try {
            $null = New-Item -ItemType Directory -Force -Path $dir
            $probe = Join-Path $dir ".probe-$PID"
            [IO.File]::WriteAllText($probe, '')
            Remove-Item -LiteralPath $probe -Force
            return (Resolve-Path -LiteralPath $dir).ProviderPath
        } catch { }
    }
    throw "no writable folder for md2pdf's runtime files (tried $($dirs -join ', ')); set MD2PDF_HOME to one"
}

function Test-Python([string]$Exe, [string[]]$Pre = @()) {
    # True when $Exe (plus $Pre, as in 'py -3') runs Python $MinPython or newer. The Microsoft Store
    # placeholder python.exe prints a hint and exits with code 9009, so it fails this test.
    if (-not $Exe) { return $false }
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $text = "$($out | Select-Object -Last 1)".Trim()
        return ($text -match '^\d+\.\d+$') -and ([version]$text -ge $MinPython)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Install-PrivatePython([string]$RuntimeHome) {
    # The official embeddable Python from python.org: a zip, no installer, no admin rights, nothing on PATH.
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $suffix = @{ AMD64 = 'amd64'; ARM64 = 'arm64' }[$arch]
    if (-not $suffix) { throw "no Python $MinPython+ found; install one from https://www.python.org" }
    $dir = Join-Path $RuntimeHome 'python'
    $tmp = Join-Path $RuntimeHome "python-setup-$PID"
    Write-Note "no Python $MinPython+ found: installing a private Python into $dir (one time, no admin needed)"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }
    $ftp = 'https://www.python.org/ftp/python/'
    $listing = (Invoke-WebRequest -Uri $ftp -UseBasicParsing).Content
    $all = @([regex]::Matches($listing, 'href="(3\.\d+\.\d+)/"') | ForEach-Object { [version]$_.Groups[1].Value })
    $url = $null
    foreach ($minor in 13, 14) {
        foreach ($v in @($all | Where-Object { $_.Minor -eq $minor } | Sort-Object -Descending)) {
            $candidate = "$ftp$v/python-$v-embed-$suffix.zip"
            try {
                $null = Invoke-WebRequest -Uri $candidate -Method Head -UseBasicParsing
                $url = $candidate
                break
            } catch { }
        }
        if ($url) { break }
    }
    if (-not $url) { throw "could not find an embeddable Python on python.org; install Python $MinPython+ yourself" }
    Remove-Item -LiteralPath $dir, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    $null = New-Item -ItemType Directory -Force -Path $tmp
    try {
        $zip = Join-Path $tmp 'python.zip'
        Write-Note "downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -LiteralPath $zip -DestinationPath $dir -Force
        # The embeddable Python ignores site-packages, where pip installs, until 'import site' is enabled in its ._pth file.
        foreach ($pth in @(Get-ChildItem -LiteralPath $dir -Filter 'python*._pth')) {
            $lines = @(Get-Content -LiteralPath $pth.FullName | ForEach-Object { $_ -replace '^\s*#\s*import site\s*$', 'import site' })
            Set-Content -LiteralPath $pth.FullName -Value $lines -Encoding Ascii
        }
        $getPip = Join-Path $tmp 'get-pip.py'
        Write-Note 'downloading pip from https://bootstrap.pypa.io/get-pip.py'
        Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing
        $python = Join-Path $dir 'python.exe'
        $saved = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $log = & $python $getPip --no-warn-script-location --disable-pip-version-check 2>&1 | Out-String
        $code = $LASTEXITCODE
        $ErrorActionPreference = $saved
        if ($code -ne 0 -or -not (Test-Python $python)) {
            Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
            throw "the private Python did not install correctly (get-pip exit code $code):`n$log"
        }
        return $python
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-Python([string]$RuntimeHome) {
    # The command that runs Python, as an array: the exe, then any extra arguments.
    $private = Join-Path $RuntimeHome 'python\python.exe'
    $choice = "$env:MD2PDF_PYTHON"
    if ($choice -eq 'private') {
        if (Test-Python $private) { return , @($private) }
        return , @(Install-PrivatePython $RuntimeHome)
    }
    if ($choice) {
        if (Test-Python $choice) { return , @($choice) }
        throw "MD2PDF_PYTHON=$choice is not Python $MinPython or newer"
    }
    if (Test-Python $private) { return , @($private) }
    $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher -and (Test-Python $launcher.Path @('-3'))) { return , @($launcher.Path, '-3') }
    foreach ($name in 'python', 'python3') {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            if (Test-Python $cmd.Path) { return , @($cmd.Path) }
        }
    }
    return , @(Install-PrivatePython $RuntimeHome)
}

function Add-ToolToPath {
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        $current = ''
        $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        if ($key.GetValueNames() -contains 'Path') {
            $current = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $kind = $key.GetValueKind('Path')
        }
        $parts = @($current -split ';' | Where-Object { $_ })
        $mine = $ToolDir.TrimEnd('\')
        if (@($parts | Where-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $mine }).Count) {
            Write-Host "md2pdf is already on your PATH ($ToolDir)."
            return
        }
        $key.SetValue('Path', (($parts + $ToolDir) -join ';'), $kind)
    } finally {
        $key.Close()
    }
    # Setting any user variable through .NET broadcasts WM_SETTINGCHANGE, so new terminals pick up the new PATH.
    [Environment]::SetEnvironmentVariable('MD2PDF_PATH_CHANGED', '1', 'User')
    [Environment]::SetEnvironmentVariable('MD2PDF_PATH_CHANGED', $null, 'User')
    $env:Path = "$env:Path;$ToolDir"
    Write-Host "Added $ToolDir to your user PATH. Open a new terminal and run: md2pdf --help"
}

function Test-StartedFromExplorer {
    # True when md2pdf.cmd was double-clicked or had files dropped on it: that console window closes at exit.
    # Explorer starts it as: cmd.exe /c ""C:\...\md2pdf.cmd" "C:\notes.md"". A double-clicked batch file that
    # calls md2pdf.cmd has its own name there instead, so it does not stop after every conversion.
    $launcher = "$env:MD2PDF_LAUNCHER"
    if (-not $launcher) { return $false }
    try {
        $me = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
        $cmd = Get-CimInstance Win32_Process -Filter "ProcessId=$($me.ParentProcessId)"
        if (-not $cmd -or $cmd.Name -ne 'cmd.exe') { return $false }
        $line = "$($cmd.CommandLine)"
        if ($line -notmatch '\s/c[\s"]' -or $line.IndexOf($launcher, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
        $top = Get-CimInstance Win32_Process -Filter "ProcessId=$($cmd.ParentProcessId)"
        return [bool]($top -and $top.Name -eq 'explorer.exe')
    } catch {
        return $false
    }
}

if ($ScriptArgs -contains '--add-to-path') {
    try { Add-ToolToPath; exit 0 } catch { Write-Note "could not change PATH: $($_.Exception.Message)"; exit 1 }
}

$code = 0
$savedHome = $env:MD2PDF_HOME
try {
    if (-not (Test-Path -LiteralPath $Engine)) { throw "md2pdf.py is missing from $ToolDir" }
    $runtime = Get-RuntimeHome
    $env:MD2PDF_HOME = $runtime
    $python = Resolve-Python $runtime
    $exe = $python[0]
    $pre = @($python | Select-Object -Skip 1)
    & $exe @pre $Engine @ScriptArgs
    $code = $LASTEXITCODE
} catch {
    Write-Note "setup problem: $($_.Exception.Message)"
    $code = 3
} finally {
    $env:MD2PDF_HOME = $savedHome
}
if (Test-StartedFromExplorer) {
    Write-Host ''
    $null = Read-Host 'Press Enter to close'
}
exit $code
