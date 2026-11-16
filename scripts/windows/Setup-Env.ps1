[CmdletBinding()]
param()

Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop

. (Join-Path $PSScriptRoot 'Launcher.Common.ps1')

$repo = Get-RepoRoot
if (-not [Environment]::Is64BitOperatingSystem) {
    Write-Host 'ERROR: this package requires 64-bit Windows.'
    exit 4
}
$pythonRuntime = Get-PythonRuntimeDir
[Environment]::SetEnvironmentVariable('PROCUREMENT_SETUP_PYTHON_RUNTIME_DIR', $pythonRuntime, 'Process')
if ($pythonRuntime -ne (Join-Path $repo '.venv')) {
    Write-Host "Using a short private Python dependency path: $pythonRuntime"
}

function Find-SupportedSystemPython {
    $candidates = @()
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) { $candidates += $pythonCmd.Source }
    $candidates += @(
        'C:\Program Files\Python312\python.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    )
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $versionText = (& $candidate --version 2>&1) -join ' '
        if ($versionText -match 'Python 3\.12(?:\.|$)') { return $candidate }
    }
    return $null
}

Push-Location $repo
try {
    $manifest = Join-Path $repo 'PACKAGE_MANIFEST.sha256'
    if (Test-Path -LiteralPath $manifest) {
        $configuredEnv = Join-Path $repo '.env'
        $allowConfiguredEnv = (
            (Test-Path -LiteralPath $configuredEnv) -and
            (Select-String -LiteralPath $configuredEnv -Pattern '^PACKAGE_LOCAL_ENV_PENDING=0$' -Quiet)
        )
        & (Join-Path $PSScriptRoot 'Verify-Package.ps1') -AllowConfiguredEnv:$allowConfiguredEnv
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'ERROR: package integrity verification failed. Extract a fresh copy and verify its SHA256 file.'
            exit 4
        }
    }

    # Prepare the signed application-local converter before .env generation.
    $bundledOffice = Join-Path $repo 'libreoffice\program\soffice.com'
    if (-not (Test-Path -LiteralPath $bundledOffice)) {
        $requiredFreeBytes = 4GB
        $availableFreeBytes = [int64](Get-Item -LiteralPath $repo).PSDrive.Free
        if ($availableFreeBytes -lt $requiredFreeBytes) {
            $availableFreeGB = [math]::Round($availableFreeBytes / 1GB, 1)
            Write-Host "ERROR: first setup requires at least 4 GB free on the installation drive ($availableFreeGB GB available)."
            Write-Host 'Free disk space, then run Setup.bat again. Existing application data is not changed.'
            exit 4
        }
        $installerRoot = Join-Path $repo 'installers'
        $officeInstaller = Get-ChildItem -LiteralPath $installerRoot -Filter 'LibreOffice_*_Win_x86-64.msi' -File -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            Select-Object -First 1
        if ($officeInstaller) {
            $signature = Get-AuthenticodeSignature -LiteralPath $officeInstaller.FullName
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*The Document Foundation*') {
                Write-Host 'ERROR: bundled LibreOffice installer signature is invalid.'
                exit 4
            }
            $officeRoot = Join-Path $repo 'libreoffice'
            $logDir = Join-Path $repo 'runtime\logs'
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
            $officeLog = Join-Path $logDir 'libreoffice-setup.log'
            Write-Host 'Preparing the bundled headless document converter...'
            $arguments = "/a `"$($officeInstaller.FullName)`" TARGETDIR=`"$officeRoot`" /qn /norestart /L*v `"$officeLog`""
            $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
            if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $bundledOffice)) {
                Write-Host "ERROR: bundled LibreOffice preparation failed (exit code $($process.ExitCode)); see $officeLog."
                exit 4
            }
        }
    }
    if (Test-Path -LiteralPath $bundledOffice) {
        $officeVersion = (& $bundledOffice --version 2>&1) -join ' '
        if ($LASTEXITCODE -ne 0 -or $officeVersion -notmatch '^LibreOffice\s+') {
            Write-Host 'ERROR: bundled LibreOffice runtime failed its startup check.'
            exit 4
        }
        Write-Host "Document converter ready: $officeVersion"
    }

    # 1. Supported Python and virtual environment ---------------------------------------------
    $python = Get-VenvPython
    if (Test-Path -LiteralPath $python) {
        Write-Host 'Using existing Python environment.'
    } else {
        $wheelhouse = Join-Path $repo 'wheelhouse'
        $bundledInstaller = Join-Path $repo 'python-3.12.10-amd64.exe'
        $privateDir = Get-PrivatePythonRuntimeDir
        $privatePython = Join-Path $privateDir 'python.exe'
        $pythonSource = $null
        if (Test-Path -LiteralPath $privatePython) {
            $pythonSource = $privatePython
        } elseif ($supportedSystemPython = Find-SupportedSystemPython) {
            $pythonSource = $supportedSystemPython
        } elseif ((Test-Path -LiteralPath $wheelhouse) -and (Test-Path -LiteralPath $bundledInstaller)) {
            $signature = Get-AuthenticodeSignature -LiteralPath $bundledInstaller
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
                Write-Host 'ERROR: bundled Python installer signature is invalid.'
                exit 4
            }
            Write-Host 'Installing the bundled private Python 3.12 runtime (system Python is unchanged)...'
            $arguments = "/quiet InstallAllUsers=0 TargetDir=`"$privateDir`" Include_pip=1 Include_launcher=0 PrependPath=0 Include_test=0 Include_doc=0 Shortcuts=0"
            $installer = Start-Process -FilePath $bundledInstaller -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
            if ($installer.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $privatePython)) {
                Write-Host "ERROR: bundled Python installation failed (exit code $($installer.ExitCode))."
                exit 4
            }
            $pythonSource = $privatePython
        }
        if (-not $pythonSource) {
            Write-Host 'ERROR: supported Python was not found and no bundled runtime is present.'
            exit 4
        }
        $versionText = (& $pythonSource --version 2>&1) -join ' '
        $minor = 0
        if ($versionText -match 'Python 3\.(\d+)') { $minor = [int]$Matches[1] }
        if ($minor -ne 12) {
            Write-Host "ERROR: Python 3.12 is required by the bundled offline wheels (found: $versionText)."
            exit 4
        }
        Write-Host "Creating Python environment with $versionText ..."
        & $pythonSource -m venv $pythonRuntime
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
            Write-Host 'ERROR: virtual environment creation failed.'
            exit 4
        }
    }
    $venvVersionText = (& $python --version 2>&1) -join ' '
    if (
        $venvVersionText -notmatch 'Python\s+(\d+)\.(\d+)' -or
        [int]$Matches[1] -lt 3 -or
        ([int]$Matches[1] -ne 3 -or [int]$Matches[2] -ne 12)
    ) {
        Write-Host "ERROR: the Python environment must use Python 3.12 (found: $venvVersionText)."
        exit 4
    }

    # 2. Dependencies and console entry points --------------------------------------------------
    $wheelhouse = Join-Path $repo 'wheelhouse'
    if (Test-Path -LiteralPath $wheelhouse) {
        Write-Host 'Installing dependencies from bundled wheelhouse (offline mode)...'
        & $python -m pip install --no-index --find-links $wheelhouse 'pip==26.1.2' 'setuptools==83.0.0'
        if ($LASTEXITCODE -eq 0) {
            & $python -m pip install --no-index --find-links $wheelhouse -r requirements.txt -e .
        }
    } else {
        Write-Host 'Installing dependencies from the configured Python package index...'
        & $python -m pip install 'pip==26.1.2' 'setuptools==83.0.0'
        if ($LASTEXITCODE -eq 0) {
            & $python -m pip install -r requirements.txt -e .
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ERROR: dependency installation failed.'
        exit 4
    }
    $entryProbe = @'
from importlib.metadata import entry_points
required = {'procurement-api', 'procurement-worker', 'procurement-verify-config', 'procurement-prepare-local-env'}
installed = {entry.name for entry in entry_points(group='console_scripts')}
missing = sorted(required - installed)
if missing:
    print('ERROR: missing application entry point metadata: ' + ', '.join(missing))
    raise SystemExit(1)
'@
    & $python -c $entryProbe
    if ($LASTEXITCODE -ne 0) { exit 4 }
    & $python -c "import app, procurement.fixed_pipeline_api, procurement.worker; print('Application import smoke test passed.')"
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ERROR: application import smoke test failed.'
        exit 4
    }

    # 3. .env: retain provider/auth settings and prepare only machine-local values ----------------
    $envFile = Join-Path $repo '.env'
    $envCreated = -not (Test-Path -LiteralPath $envFile)
    if ($envCreated) {
        Write-Host 'Creating .env from .env.example and generating local secrets (values are never printed)...'
    } else {
        Write-Host 'Preparing machine-local .env values; provider and login credentials are retained.'
    }
    & $python -m procurement.prepare_env
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ERROR: .env preparation failed.'
        exit 2
    }
    Import-PortablePackageEnvironment
    if ($envCreated) {
        Write-Host '.env created. Edit it now: AI provider URL, model, and API key.'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    # The owner needs Modify so the credential tool can atomically replace the file.
    & icacls $envFile /inheritance:r /grant:r "${identity}:(M)" '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ERROR: could not restrict .env permissions to the installation user, SYSTEM, and Administrators.'
        exit 2
    }
    Write-Host 'Restricting installation, runtime, PID, and log permissions...'
    # Grant explicit access before removing inherited permissions so setup can
    # never lock out the installation account between ACL operations.
    & icacls $repo /grant:r "${identity}:(OI)(CI)(M)" '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null
    if ($LASTEXITCODE -eq 0) {
        & icacls $repo /inheritance:r | Out-Null
    }
    if ($LASTEXITCODE -eq 0) {
        & icacls $repo /remove:g '*S-1-5-11' /T | Out-Null
    }
    if ($LASTEXITCODE -eq 0) {
        & icacls $repo /grant:r "${identity}:(OI)(CI)(M)" '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' /T | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ERROR: could not restrict installation permissions to the installation user, SYSTEM, and Administrators.'
        exit 2
    }
    $externalPythonRoot = Split-Path (Get-PythonRuntimeDir) -Parent
    if (-not $externalPythonRoot.StartsWith($repo, [StringComparison]::OrdinalIgnoreCase)) {
        & icacls $externalPythonRoot /grant:r "${identity}:(OI)(CI)(M)" '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null
        if ($LASTEXITCODE -eq 0) { & icacls $externalPythonRoot /inheritance:r | Out-Null }
        if ($LASTEXITCODE -eq 0) { & icacls $externalPythonRoot /remove:g '*S-1-5-11' /T | Out-Null }
        if ($LASTEXITCODE -eq 0) {
            & icacls $externalPythonRoot /grant:r "${identity}:(OI)(CI)(M)" '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' /T | Out-Null
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'ERROR: could not restrict the external Python runtime permissions.'
            exit 2
        }
    }
    $bundledDatabase = Join-Path $PSScriptRoot 'Portable-Database.ps1'
    $useBundledDatabase = (Get-EffectiveValue -Key 'LOCAL_POSTGRES_ENABLED') -ne '0'
    if ($useBundledDatabase -and (Test-Path -LiteralPath (Join-Path $repo 'postgresql\bin\postgres.exe'))) {
        Write-Host 'Preparing the bundled local PostgreSQL database...'
        & $bundledDatabase
        if ($LASTEXITCODE -ne 0) { exit 4 }
    }
    if ($envCreated) {
        Write-Host ''
        Write-Host 'Setup phase 1 complete. Edit the AI/OCR settings in .env, then run Setup.bat again.'
        exit 0
    }

    # 4. Use the bundled database when present; otherwise require PostgreSQL 15+ -----------------
    $psql = $null
    $bundledPsql = Join-Path $repo 'postgresql\bin\psql.exe'
    if ($useBundledDatabase -and (Test-Path -LiteralPath $bundledPsql)) {
        $psql = $bundledPsql
    } else {
        $psqlCmd = Get-Command psql -ErrorAction SilentlyContinue
        if ($psqlCmd) { $psql = $psqlCmd.Source }
    }
    if (-not $psql -and (Test-Path -LiteralPath 'C:\Program Files\PostgreSQL')) {
        $candidate = Get-ChildItem 'C:\Program Files\PostgreSQL' -Directory -ErrorAction SilentlyContinue |
            Sort-Object { [int]($_.Name -replace '\D', '') } -Descending | Select-Object -First 1
        if ($candidate) {
            $maybe = Join-Path $candidate.FullName 'bin\psql.exe'
            if (Test-Path -LiteralPath $maybe) { $psql = $maybe }
        }
    }
    if (-not $psql) {
        Write-Host 'ERROR: PostgreSQL 15+ is required but no bundled or installed runtime was found.'
        exit 4
    }
    $pgVersion = (& $psql --version 2>&1) -join ' '
    $pgMajor = 0
    if ($pgVersion -match '(\d+)\.\d+' ) { $pgMajor = [int]$Matches[1] }
    elseif ($pgVersion -match '(\d+)') { $pgMajor = [int]$Matches[1] }
    if ($pgMajor -lt 15) {
        Write-Host "ERROR: PostgreSQL 15+ is required; detected: $pgVersion"
        exit 4
    }
    Write-Host "PostgreSQL detected: $pgVersion"
    $pgService = Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '*postgres*' } | Select-Object -First 1
    if ($pgService -and $pgService.Status -ne 'Running') {
        Write-Host "WARN: Windows service $($pgService.Name) is $($pgService.Status); start it before Start_All.bat."
    }
    $dbUrl = Get-EffectiveValue -Key 'DATABASE_URL'
    if (-not $dbUrl -or $dbUrl -match 'replace_') {
        Write-Host 'ERROR: DATABASE_URL is missing or still a placeholder (value is never printed).'
        exit 2
    }
    Write-Host 'Testing DATABASE_URL and PostgreSQL server version...'
    $probe = @'
from sqlalchemy import create_engine, text
from procurement.platform.database import database_url
try:
    engine = create_engine(database_url())
    with engine.connect() as connection:
        connection.execute(text('SELECT 1')).scalar_one()
        version_num = int(connection.execute(text('SHOW server_version_num')).scalar_one())
    engine.dispose()
except Exception:
    print('ERROR: DATABASE_URL connection test failed; verify PostgreSQL is running and the configured database/user exists.')
    raise SystemExit(1)
if version_num < 150000:
    print('ERROR: PostgreSQL server 15+ is required.')
    raise SystemExit(1)
print(f'PostgreSQL connection verified (server major {version_num // 10000}).')
'@
    & $python -c $probe
    if ($LASTEXITCODE -ne 0) { exit 4 }

    Write-Host ''
    Write-Host 'Setup complete. Next: edit .env if needed, then run Start_All.bat.'
    exit 0
} finally { Pop-Location }
