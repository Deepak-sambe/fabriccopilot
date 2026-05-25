#!/usr/bin/env python3
"""
SQL MCP Server — Python / FastMCP
Connects to Microsoft Fabric Warehouse via pyodbc + AAD token.
Auth priority: Service Principal (hardcoded) → Azure CLI session
"""

import sys, os, json, struct, subprocess, warnings
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────
#  HARDCODED CONFIG — edit these once, works on any machine
# ─────────────────────────────────────────────────────────────
SERVER      = "3zje2kmflizevlwnfo6d7t4f2q-dq37ewilsz7uvcth5w7fsdcwye.datawarehouse.fabric.microsoft.com"
DATABASE    = "PPOServe_Bronze_Silver_WH"
DRIVER      = "ODBC Driver 18 for SQL Server"
READONLY    = True   # set False to allow INSERT/UPDATE/DELETE

# Service Principal credentials (from Azure App Registration)
# These allow the server to run on ANY machine without az login
CLIENT_ID     = "dfc8a932-744c-45e0-aa96-640f9a182b65"
CLIENT_SECRET = "why8Q~C0urWwrxDLY0UfHNeCapVraxLG20CuNbXA"
TENANT_ID     = "294d52de-5a85-4a32-aecd-2bbc3fcf85d4"
# ─────────────────────────────────────────────────────────────

from fastmcp import FastMCP
import pyodbc

mcp = FastMCP("sql-fabric-mcp")

# ── Token acquisition ────────────────────────────────────────

def get_token_from_sp() -> str:
    """Get AAD token using Service Principal credentials (works on any machine)."""
    import urllib.request, urllib.parse
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope":         "https://database.windows.net/.default",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


def get_token_from_az_cli() -> str:
    """Fallback: get token from existing az login session."""
    result = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", "https://database.windows.net/"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"az CLI token failed: {result.stderr}")
    return json.loads(result.stdout)["accessToken"]


def get_token() -> str:
    """Try SP first, fall back to az CLI."""
    if CLIENT_ID and CLIENT_SECRET and TENANT_ID:
        try:
            return get_token_from_sp()
        except Exception as e:
            print(f"[WARN] SP token failed ({e}), trying az CLI...", file=sys.stderr)
    return get_token_from_az_cli()


def pack_token(token: str) -> bytes:
    """Pack token into the format pyodbc expects for SQL_COPT_SS_ACCESS_TOKEN."""
    token_bytes = token.encode("UTF-16-LE")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


def get_connection() -> pyodbc.Connection:
    """Open a fresh pyodbc connection to Fabric Warehouse."""
    token_struct = pack_token(get_token())
    conn_str = (
        f"Driver={{{DRIVER}}};"
        f"Server={SERVER},1433;"
        f"Database={DATABASE};"
        f"Encrypt=yes;TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, attrs_before={1256: token_struct})


# ── Tools ────────────────────────────────────────────────────

@mcp.tool()
def list_tables(schema: str = "") -> dict:
    """
    List all tables in the Fabric Warehouse.
    Optionally filter by schema name (e.g. 'dbo').
    Always call this first before building any SQL query.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if schema:
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                "FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = ? ORDER BY TABLE_SCHEMA, TABLE_NAME",
                schema
            )
        else:
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                "FROM INFORMATION_SCHEMA.TABLES "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME"
            )
        rows = cursor.fetchall()
        conn.close()
        return {
            "tables": [
                {"schema": r[0], "name": r[1], "type": r[2]} for r in rows
            ],
            "count": len(rows)
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def describe_table(table_name: str, schema: str = "dbo") -> dict:
    """
    Get the full column schema of a table — names, data types, nullability.
    Always call this before writing a query against an unfamiliar table.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
            "IS_NULLABLE, COLUMN_DEFAULT "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ? "
            "ORDER BY ORDINAL_POSITION",
            table_name, schema
        )
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return {"error": f"Table '{schema}.{table_name}' not found or has no columns."}
        return {
            "table": f"{schema}.{table_name}",
            "columns": [
                {
                    "name":     r[0],
                    "type":     r[1] + (f"({r[2]})" if r[2] else ""),
                    "nullable": r[3],
                    "default":  r[4],
                }
                for r in rows
            ]
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def read_data(query: str) -> dict:
    """
    Execute a SELECT query against the Fabric Warehouse and return results.
    Only SELECT queries are allowed. Results are capped at 500 rows.
    IMPORTANT: Always call list_tables and describe_table first to get
    the correct table/column names before calling this tool.
    """
    if READONLY:
        q = query.strip().upper()
        forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE",
                     "ALTER", "TRUNCATE", "EXEC", "EXECUTE"]
        for kw in forbidden:
            if q.startswith(kw) or f" {kw} " in q:
                return {"error": f"Readonly mode: '{kw}' statements are not allowed."}
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchmany(500)
        conn.close()
        return {
            "columns": columns,
            "rows":    [dict(zip(columns, row)) for row in rows],
            "count":   len(rows),
            "capped":  len(rows) == 500,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Run ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("[MCP] SQL Fabric MCP Server starting...", file=sys.stderr)
    print(f"[MCP] Server  : {SERVER}", file=sys.stderr)
    print(f"[MCP] Database: {DATABASE}", file=sys.stderr)
    print(f"[MCP] Readonly: {READONLY}", file=sys.stderr)
    port = int(os.environ.get("DATABRICKS_APP_PORT", 3000))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
