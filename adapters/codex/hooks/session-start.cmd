@echo off
setlocal
call "%~dp0find-git-bash.cmd"
if not defined VIBE_BASH_EXE (
  echo {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "vibe-cognition: Git for Windows (Git Bash) is required for this plugin's hooks on Windows but was not found. INSTRUCTION: tell the user to install it from https://git-scm.com/download/win and restart Codex."}}
  exit /b 0
)
set "VIBE_SH=%~dp0session-start.sh"
set "VIBE_SH=%VIBE_SH:\=/%"
"%VIBE_BASH_EXE%" "%VIBE_SH%"
exit /b %ERRORLEVEL%
