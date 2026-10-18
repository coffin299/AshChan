@echo off
REM ============================================================
REM AshChan RAG bootstrap (LM Studio)
REM Indexes a local VRCast checkout using embeddings from LM Studio.
REM All messages in this file are intentionally in English.
REM ============================================================
setlocal EnableExtensions

cd /d "%~dp0"

echo.
echo === AshChan RAG Bootstrap (LM Studio) ===
echo.

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv not found. Run start.bat once to create the virtualenv.
  exit /b 1
)

if "%VRCAST_REPO_PATH%"=="" (
  set /p VRCAST_REPO_PATH=Enter local VRCast repo path: 
)
if "%VRCAST_REPO_PATH%"=="" (
  echo ERROR: VRCAST_REPO_PATH is empty.
  exit /b 1
)
if not exist "%VRCAST_REPO_PATH%" (
  echo ERROR: Repo path does not exist: %VRCAST_REPO_PATH%
  exit /b 1
)

if "%RAG_TAG%"=="" (
  set /p RAG_TAG=Enter release tag to record ^(leave empty for latest GitHub release^): 
)

if "%RAG_BASE_URL%"=="" set RAG_BASE_URL=http://127.0.0.1:1234/v1
if "%RAG_API_KEY%"=="" set RAG_API_KEY=lm-studio
if "%RAG_EMBED_MODEL%"=="" set RAG_EMBED_MODEL=text-embedding-nomic-embed-text-v1.5

echo.
echo Repo:   %VRCAST_REPO_PATH%
echo Tag:    %RAG_TAG%
echo API:    %RAG_BASE_URL%
echo Model:  %RAG_EMBED_MODEL%
echo.
echo Make sure LM Studio local server is running with an embedding model loaded.
echo.

set "TAG_ARGS="
if not "%RAG_TAG%"=="" set "TAG_ARGS=--tag %RAG_TAG%"

".venv\Scripts\python.exe" -m ash.rag bootstrap --provider local --force --repo-path "%VRCAST_REPO_PATH%" %TAG_ARGS%
set ERR=%ERRORLEVEL%

if %ERR% neq 0 (
  echo.
  echo ERROR: Bootstrap failed with exit code %ERR%.
  echo Check LM Studio server, model name, and repo path.
  exit /b %ERR%
)

echo.
echo SUCCESS: RAG index written to data\ashchan.db
echo Next for production: use /ash rag rebuild-nim so query embeddings match NIM.
echo.
exit /b 0
