@echo off
rem goose-dap on Windows: the Goose DAP proxy in front of lldb-dap
rem (goose_dap.py beside this file, run by Python 3; PYTHON picks another).
setlocal
if "%PYTHON%"=="" (set "PY=python") else (set "PY=%PYTHON%")
"%PY%" -u "%~dp0goose_dap.py" %*
