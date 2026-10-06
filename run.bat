@echo off
rem Lance Prospection : prepare l'environnement au premier passage, puis ouvre le navigateur.
rem Laisse cette fenetre ouverte tant que tu utilises l'application ; ferme-la pour l'arreter.
cd /d "%~dp0"
title Prospection

if not exist ".venv\Scripts\python.exe" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo.
    echo Python est introuvable. Installe Python 3.12 ou plus depuis python.org ^(coche "Add python.exe to PATH"^), puis relance ce fichier.
    pause
    exit /b 1
  )
  echo Premier lancement : preparation de l'environnement ^(une minute environ^)...
  python -m venv .venv
)

".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
".venv\Scripts\python.exe" app.py

if errorlevel 1 (
  echo.
  echo L'application s'est arretee avec une erreur : lis le message ci-dessus.
  pause
)
