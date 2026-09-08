@echo off
rem Sets VIBE_BASH_EXE to Git Bash (never the WSL launcher in System32).
set "VIBE_BASH_EXE="
set "VIBE_GIT_EXE="
for /f "delims=" %%G in ('where git.exe 2^>nul') do if not defined VIBE_GIT_EXE set "VIBE_GIT_EXE=%%G"
if defined VIBE_GIT_EXE for %%G in ("%VIBE_GIT_EXE%\..\..\bin\bash.exe") do if exist "%%~fG" set "VIBE_BASH_EXE=%%~fG"
if not defined VIBE_BASH_EXE if exist "%ProgramFiles%\Git\bin\bash.exe" set "VIBE_BASH_EXE=%ProgramFiles%\Git\bin\bash.exe"
if not defined VIBE_BASH_EXE if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "VIBE_BASH_EXE=%LocalAppData%\Programs\Git\bin\bash.exe"
if not defined VIBE_BASH_EXE if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "VIBE_BASH_EXE=%ProgramFiles(x86)%\Git\bin\bash.exe"
