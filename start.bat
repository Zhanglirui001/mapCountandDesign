@echo off
where uv >nul 2>nul
if %errorlevel% neq 0 (
    powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%UserProfile%\.cargo\bin;%USERPROFILE%\.local\bin;%PATH%"
    call "%0" %*
    exit /b
)
uv sync
uv run python -m count_and_design
