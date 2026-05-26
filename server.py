#!/usr/bin/env python3
"""
SQL MCP Server — Python / FastMCP
Connects to Microsoft Fabric Warehouse via MSAL Device Code auth.
Same pattern as PowerBI MCP — login once, token cached, auto-refreshed.
"""

import sys, os, json, warnings, io, contextlib, logging
warnings.filterwarnings("ignore")
os.environ['PYTHONWARNINGS'] = 'ignore'

from fastmcp import FastMCP
import msal
import pytds
from datetime import datetime

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)
logging.getLogger('msal').setLevel(logging.WARNING)

def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)

# ── Config ───────────────────────────────────────────────────
SERVER   = "3zje2kmflizevlwnfo6d7t4f2q-dq37ewilsz7uvcth5w7fsdcwye.datawarehouse.fabric.microsoft.com"
DATABASE = "PPOServe_Bronze_Silver_WH"
READONLY = True

# App registration — must have "Allow public client flows" enabled in Azure
CLIENT_ID = "dfc8a932-744c-45e0-aa96-640f9a182b65"
TENANT_ID = "294d52de-5a85-4a32-aecd-2bbc3fcf85d4"
SCOPES    = ["https://database.windows.net/.default"]

# Token cache location
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
AUTH_DIR      = os.path.join(SCRIPT_DIR, "AUTH_STATUS")
TOKEN_CACHE   = os.path.join(AUTH_DIR, "fabric_token_cache.json")
FLOW_CACHE    = os.path.join(AUTH_DIR, "fabric_auth_flow.json")
os.makedirs(AUTH_DIR, exist_ok=True)

mcp = FastMCP("sql-fabric-mcp")

# ── Token cache helpers ───────────────────────────────────────

def load_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE):
        with open(TOKEN_CACHE) as f:
            cache.deserialize(f.read())
    return cache

def save_cache(cache):
    if cache.has_state_changed:
        with open(TOKEN_CACHE, "w") as f:
            f.write(cache.serialize())

def get_msal_app(cache=None):
    if cache is None:
        cache = load_cache()
    with contextlib.redirect_stdout(io.StringIO()):
        app = msal.PublicClientApplication(
            CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{TENANT_ID}",
            token_cache=cache
        )
    return app

def get_token_silent() -> str | None:
    """Try to get token from cache without user interaction."""
    cache = load_cache()
    app   = get_msal_app(cache)
    accounts = app.get_accounts()
    if accounts:
        with contextlib.redirect_stdout(io.StringIO()):
            result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    return None

def get_connection():
    """Open a fresh pytds connection using cached token (no ODBC driver needed)."""
    token = get_token_silent()
    if not token:
        raise RuntimeError("NOT_AUTHENTICATED")
    return pytds.connect(
        dsn=SERVER,
        database=DATABASE,
        port=1433,
        auth=pytds.login.MsAccessTokenLogin(token=token),
        encryption=True,
    )

# ── Auth tools ───────────────────────────────────────────────

@mcp.tool()
def get_authentication_status() -> dict:
    """
    Check if the user is authenticated to Fabric Warehouse.
    Call this at the start of each conversation to check login status.
    """
    try:
        if not os.path.exists(TOKEN_CACHE):
            return {
                "authenticated": False,
                "status": "not_logged_in",
                "message": "Not logged in. Please use 'authenticate_fabric' to sign in.",
            }
        cache = load_cache()
        app   = get_msal_app(cache)
        accounts = app.get_accounts()
        if not accounts:
            return {
                "authenticated": False,
                "status": "no_account",
                "message": "No account found. Please use 'authenticate_fabric' to sign in.",
            }
        with contextlib.redirect_stdout(io.StringIO()):
            result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return {
                "authenticated": True,
                "status": "ready",
                "message": "Authenticated and ready to query Fabric Warehouse.",
                "account": accounts[0].get("username", "Unknown"),
            }
        return {
            "authenticated": False,
            "status": "token_expired",
            "message": "Session expired. Please use 'authenticate_fabric' to sign in again.",
            "account": accounts[0].get("username", "Unknown"),
        }
    except Exception as e:
        return {"authenticated": False, "status": "error", "message": str(e)}


@mcp.tool()
def authenticate_fabric() -> dict:
    """
    Start the Fabric Warehouse login process using a device code.
    Shows a code and URL — the user opens the URL, enters the code, and signs in.
    After signing in, call 'complete_authentication' to finish.
    """
    try:
        cache = load_cache()
        app   = get_msal_app(cache)
        with contextlib.redirect_stdout(io.StringIO()):
            flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            return {"success": False, "error": "Failed to initiate device flow."}

        # Save flow for completion step
        with open(FLOW_CACHE, "w") as f:
            json.dump(flow, f)

        log(f"Device code: {flow['user_code']}")
        return {
            "success": True,
            "authentication_required": True,
            "device_code": flow["user_code"],
            "verification_url": flow.get("verification_uri", "https://microsoft.com/devicelogin"),
            "expires_in_minutes": flow.get("expires_in", 900) // 60,
            "instructions": [
                "1. Open your browser",
                "2. Go to: " + flow.get("verification_uri", "https://microsoft.com/devicelogin"),
                f"3. Enter this code: {flow['user_code']}",
                "4. Sign in with your Microsoft work account",
                "5. Come back and use 'complete_authentication' to finish",
            ],
            "message": f"Go to {flow.get('verification_uri', 'https://microsoft.com/devicelogin')} and enter code: {flow['user_code']}",
        }
    except Exception as e:
        log(f"Auth error: {e}")
        return {"success": False, "error": str(e)}


@mcp.tool()
def complete_authentication() -> dict:
    """
    Complete login after the user has entered the device code in their browser.
    Call this after 'authenticate_fabric' once you've signed in.
    """
    try:
        if not os.path.exists(FLOW_CACHE):
            return {
                "success": False,
                "error": "No pending login found. Please use 'authenticate_fabric' first.",
            }
        with open(FLOW_CACHE) as f:
            flow = json.load(f)

        cache = load_cache()
        app   = get_msal_app(cache)
        with contextlib.redirect_stdout(io.StringIO()):
            result = app.acquire_token_by_device_flow(flow)

        if os.path.exists(FLOW_CACHE):
            os.remove(FLOW_CACHE)

        if "access_token" in result:
            save_cache(cache)
            username = result.get("id_token_claims", {}).get("preferred_username", "Unknown")
            log(f"Authenticated: {username}")
            return {
                "success": True,
                "authenticated": True,
                "message": f"Login successful! Signed in as {username}. You can now query Fabric Warehouse.",
                "username": username,
            }
        else:
            error = result.get("error_description", "Unknown error")
            if "pending" in error.lower() or "authorization_pending" in result.get("error", ""):
                return {
                    "success": False,
                    "pending": True,
                    "message": "Login not completed yet. Please finish signing in your browser, then try 'complete_authentication' again.",
                }
            return {"success": False, "error": error}
    except Exception as e:
        log(f"Complete auth error: {e}")
        return {"success": False, "error": str(e)}


# ── SQL tools ────────────────────────────────────────────────

def _auth_guard():
    """Returns error dict if not authenticated, else None."""
    if not get_token_silent():
        return {
            "error": "Not authenticated",
            "message": "Please sign in first using 'authenticate_fabric', then 'complete_authentication'.",
            "action_required": "authentication",
        }
    return None


@mcp.tool()
def list_tables(schema: str = "") -> dict:
    """
    List all tables in the Fabric Warehouse. Optionally filter by schema (e.g. 'etl_monitoring').
    Always call this first before building any SQL query.
    """
    guard = _auth_guard()
    if guard: return guard
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        if schema:
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                "FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = %s "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME", (schema,)
            )
        else:
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                "FROM INFORMATION_SCHEMA.TABLES ORDER BY TABLE_SCHEMA, TABLE_NAME"
            )
        rows = cursor.fetchall()
        conn.close()
        return {"tables": [{"schema": r[0], "name": r[1], "type": r[2]} for r in rows], "count": len(rows)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def describe_table(table_name: str, schema: str = "dbo") -> dict:
    """
    Get columns, types and nullability for a table.
    Always call this before writing a query against an unfamiliar table.
    """
    guard = _auth_guard()
    if guard: return guard
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = %s AND TABLE_SCHEMA = %s "
            "ORDER BY ORDINAL_POSITION",
            (table_name, schema)
        )
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return {"error": f"Table '{schema}.{table_name}' not found or has no columns."}
        return {
            "table": f"{schema}.{table_name}",
            "columns": [
                {"name": r[0], "type": r[1] + (f"({r[2]})" if r[2] else ""), "nullable": r[3], "default": r[4]}
                for r in rows
            ],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def read_data(query: str) -> dict:
    """
    Execute a SELECT query against Fabric Warehouse. Results capped at 500 rows.
    Always call list_tables and describe_table first to get correct table/column names.
    """
    guard = _auth_guard()
    if guard: return guard
    if READONLY:
        q = query.strip().upper()
        for kw in ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE", "EXEC", "EXECUTE"]:
            if q.startswith(kw) or f" {kw} " in q:
                return {"error": f"Readonly mode: '{kw}' statements are not allowed."}
    try:
        conn   = get_connection()
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [col[0] for col in cursor.description]
        rows    = cursor.fetchmany(500)
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
    log("SQL Fabric MCP Server starting...")
    log(f"Server  : {SERVER}")
    log(f"Database: {DATABASE}")
    log(f"Readonly: {READONLY}")
    port = int(os.environ.get("DATABRICKS_APP_PORT", 3000))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
