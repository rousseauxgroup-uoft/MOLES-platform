@echo off
rem Sets up MOLES's private copy of Python inside the install folder.
rem Run by the installer (moles.iss) after it has copied its files:
rem     install_env.cmd "<install folder>" "<MOLES version>"
rem Downloads Python and the pinned packages (installer\requirements.txt), so
rem it needs the internet. Everything it writes stays inside the install
rem folder, which the uninstaller removes; ~\.moles (settings, boards, logs)
rem is never touched. Progress and errors go to install-log.txt.
setlocal

set "APP=%~1"
set "VERSION=%~2"
set "SETUP=%APP%\setup"
set "RUNTIME=%APP%\runtime"
set "LOG=%APP%\install-log.txt"
set "UV=%SETUP%\uv.exe"

rem Keep uv away from any settings or Pythons the user already has.
set "UV_NO_CONFIG=1"
set "UV_PYTHON_INSTALL_DIR=%APP%\python"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_CACHE_DIR=%TEMP%\moles-uv-cache"
set "UV_LINK_MODE=copy"
for /f "usebackq delims=" %%V in ("%SETUP%\python-version.txt") do set "PYVER=%%V"

echo MOLES %VERSION% setup, %DATE% %TIME% > "%LOG%"

rem Remove the previous version's Python, plus folders the first test
rem installers set aside. A MOLES window that is still open can keep files
rem in use, and installing over them would leave a broken mix of versions.
for %%D in ("%RUNTIME%.old" "%APP%\python.old" "%RUNTIME%" "%APP%\python") do (
    if exist "%%~D" rmdir /s /q "%%~D" >> "%LOG%" 2>&1
)
if exist "%RUNTIME%" (
    echo Could not remove the previous MOLES Python. Is a MOLES window still open? >> "%LOG%"
    goto :fail
)

echo. >> "%LOG%" & echo == Python %PYVER% >> "%LOG%"
rem --no-bin / --no-registry: leave the user's PATH folder and the Windows
rem registry alone; this Python is for MOLES only.
"%UV%" python install --no-bin --no-registry %PYVER% >> "%LOG%" 2>&1 || goto :fail
rem uv keeps Pythons in versioned folders; move the real one (not uv's
rem shortcut links, which "dir /a:d-l" leaves out) to a fixed name.
for /f "usebackq delims=" %%P in (`dir /b /a:d-l "%APP%\python\cpython-*"`) do (
    move "%APP%\python\%%P" "%RUNTIME%" >> "%LOG%" 2>&1 || goto :fail
)
if not exist "%RUNTIME%\python.exe" goto :fail
rmdir /s /q "%APP%\python"

echo. >> "%LOG%" & echo == Packages >> "%LOG%"
"%UV%" pip install --python "%RUNTIME%\python.exe" --break-system-packages ^
    --require-hashes --only-binary :all: --compile-bytecode ^
    --requirement "%SETUP%\requirements.txt" >> "%LOG%" 2>&1 || goto :fail
for %%W in ("%SETUP%\moles-*.whl") do (
    "%UV%" pip install --python "%RUNTIME%\python.exe" --break-system-packages ^
        --no-deps --compile-bytecode "%%~W" >> "%LOG%" 2>&1 || goto :fail
)

rem Lets the launcher know it was installed this way, so it offers updates.
> "%RUNTIME%\moles-install.json" echo {"installer": "windows", "version": "%VERSION%"}

echo. >> "%LOG%" & echo == Check >> "%LOG%"
"%RUNTIME%\python.exe" -c "import moles.ui.launcher.main_window, moles; print('MOLES', moles.__version__, 'ready')" >> "%LOG%" 2>&1 || goto :fail

if exist "%UV_CACHE_DIR%" rmdir /s /q "%UV_CACHE_DIR%"
exit /b 0

:fail
echo. >> "%LOG%"
echo == FAILED >> "%LOG%"
if exist "%UV_CACHE_DIR%" rmdir /s /q "%UV_CACHE_DIR%"
exit /b 1
