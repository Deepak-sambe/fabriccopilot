#!/bin/bash
set -e

echo "[STARTUP] Installing ODBC Driver 18 for SQL Server..."
curl -sSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
curl -sSL https://packages.microsoft.com/config/ubuntu/22.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list > /dev/null
sudo apt-get update -qq
ACCEPT_EULA=Y sudo apt-get install -y -qq msodbcsql18 unixodbc-dev

echo "[STARTUP] Installing Python dependencies..."
pip install fastmcp pyodbc --quiet

echo "[STARTUP] Starting MCP server..."
python server.py
