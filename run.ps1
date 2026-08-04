# Windows PowerShell equivalent of run.sh
# Runs the API with hot reload for local development.
# Usage:  .\run.ps1
#
# --reload-dir app  restricts file-watching to the app/ source folder only.
# Without this, uvicorn watches the whole project directory by default,
# INCLUDING local_bucket/ — which the app itself writes to on every
# create/read/update. That causes the reloader to restart the server on
# every request, killing in-flight connections (symptom: requests hang
# and eventually time out, right after the first successful write).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
uvicorn app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8000
