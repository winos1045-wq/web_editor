#!/usr/bin/env python3
"""
Web Shell – E2B Edition
Replaces the local PTY with an E2B cloud sandbox PTY.

Install deps:
    pip install fastapi uvicorn e2b

Set your API key before running:
    export E2B_API_KEY="your_key_here"

Run:
    python app.py
Visit:
    http://localhost:8000
"""

import asyncio
import json
import os
import struct
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.requests import Request
import uvicorn

# ── E2B ──────────────────────────────────────────────────────────────────────
from e2b import AsyncSandbox

try:
    from e2b.sandbox.pty.main import PtySize
except ImportError:
    # Fallback for older SDK layouts
    from e2b import PtySize  # type: ignore

app = FastAPI()

# One shared sandbox for all connections (created on startup, killed on shutdown)
_sandbox: AsyncSandbox | None = None


@app.on_event("startup")
async def startup():
    global _sandbox
    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        print("[WARN] E2B_API_KEY not set – sandbox creation will likely fail.")
    _sandbox = await AsyncSandbox.create(
        timeout=3600,   # 1-hour max (Pro: 24 h)
        api_key=api_key,
    )
    print(f"[E2B] Sandbox ready: {_sandbox.sandbox_id}")


@app.on_event("shutdown")
async def shutdown():
    global _sandbox
    if _sandbox:
        await _sandbox.kill()
        print("[E2B] Sandbox killed.")


def get_sandbox() -> AsyncSandbox:
    if _sandbox is None:
        raise RuntimeError("Sandbox not initialised yet")
    return _sandbox


# ── File API (proxied through E2B filesystem) ─────────────────────────────────

@app.get("/api/files")
async def list_files(path: str = "/"):
    sbx = get_sandbox()
    try:
        entries_raw = await sbx.filesystem.list(path)
        entries = []
        for item in sorted(entries_raw, key=lambda x: (not x.is_dir, x.name.lower())):
            entries.append({
                "name": item.name,
                "path": item.path,
                "is_dir": item.is_dir,
                "size": getattr(item, "size", 0) or 0,
            })
        return JSONResponse({"path": path, "entries": entries})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/file")
async def read_file(path: str):
    sbx = get_sandbox()
    try:
        content = await sbx.filesystem.read(path)
        # read() may return bytes or str depending on SDK version
        if isinstance(content, (bytes, bytearray)):
            content = content.decode("utf-8", errors="replace")
        ext = Path(path).suffix.lstrip(".").lower()
        return JSONResponse({"path": path, "content": content, "ext": ext})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/file")
async def write_file(request: Request):
    sbx = get_sandbox()
    try:
        body = await request.json()
        path = body.get("path")
        content = body.get("content", "")
        if not path:
            return JSONResponse({"error": "No path"}, status_code=400)
        await sbx.filesystem.write(path, content)
        return JSONResponse({"ok": True, "path": path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── HTML (unchanged UI) ────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Web Shell · E2B</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm/css/xterm.css" />
    <style>
        *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

        :root {
            --bg-base:       #0e0e0e;
            --bg-titlebar:   #161616;
            --bg-sidebar:    #111111;
            --bg-terminal:   #141414;
            --bg-editor:     #000000;
            --bg-explorer:   #0f0f0f;
            --bg-statusbar:  #0d0d0d;
            --bg-tab-active: #1c1c1c;
            --bg-hover:      rgba(255,255,255,0.06);

            --border:        rgba(255,255,255,0.08);
            --border-light:  rgba(255,255,255,0.05);

            --text-primary:  #d4d4d4;
            --text-muted:    #6b6b6b;
            --text-dim:      #444;

            --accent-green:  #4ade80;
            --accent-blue:   #60a5fa;
            --accent-purple: #a78bfa;
            --accent-yellow: #fbbf24;

            --dot-red:    #ff5f57;
            --dot-yellow: #febc2e;
            --dot-green:  #28c840;

            --radius-sm: 5px;
            --radius-md: 8px;
            --explorer-w: 240px;
        }

        html, body { height: 100%; background: #0a0a0a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 13px; color: var(--text-primary); }

        #app { height: 100vh; display: flex; flex-direction: column; background: var(--bg-base); border: 0.5px solid var(--border); }

        /* ── Title bar ── */
        #titlebar { display: flex; align-items: center; gap: 10px; padding: 0 14px; height: 40px; background: var(--bg-titlebar); border-bottom: 0.5px solid var(--border); flex-shrink: 0; user-select: none; }
        .dots { display: flex; gap: 6px; }
        .dot  { width: 12px; height: 12px; border-radius: 50%; cursor: pointer; }
        .dot-r { background: var(--dot-red); } .dot-y { background: var(--dot-yellow); } .dot-g { background: var(--dot-green); }

        /* ── Tab bar ── */
        #tabs { display: flex; align-items: flex-end; gap: 2px; margin-left: 10px; flex: 1; height: 100%; overflow-x: auto; overflow-y: hidden; scrollbar-width: none; }
        #tabs::-webkit-scrollbar { display: none; }
        .tab { display: flex; align-items: center; gap: 7px; padding: 0 12px; height: 30px; border-radius: var(--radius-sm) var(--radius-sm) 0 0; font-size: 12px; color: var(--text-muted); cursor: pointer; border: 0.5px solid transparent; background: transparent; transition: color .12s, background .12s; white-space: nowrap; flex-shrink: 0; }
        .tab.active { color: var(--text-primary); background: var(--bg-tab-active); border-color: var(--border); border-bottom-color: var(--bg-tab-active); }
        .tab:not(.active):hover { color: var(--text-primary); background: var(--bg-hover); }
        .tab-icon { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
        .tab-icon.terminal { background: var(--accent-green); }
        .tab-icon.file { background: var(--accent-blue); }
        .tab-icon.unsaved { background: var(--accent-yellow); }
        .tab-close { width: 14px; height: 14px; display: flex; align-items: center; justify-content: center; border-radius: 3px; color: var(--text-dim); font-size: 11px; line-height: 1; transition: color .1s, background .1s; }
        .tab-close:hover { color: var(--text-primary); background: rgba(255,255,255,.1); }
        .tab-add { display: flex; align-items: center; justify-content: center; width: 26px; height: 26px; border-radius: var(--radius-sm); color: var(--text-muted); font-size: 18px; cursor: pointer; margin-bottom: 2px; transition: color .1s, background .1s; flex-shrink: 0; }
        .tab-add:hover { color: var(--text-primary); background: var(--bg-hover); }

        .tb-actions { display: flex; gap: 6px; align-items: center; margin-left: auto; }
        .tb-btn { width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; border-radius: var(--radius-sm); color: var(--text-muted); cursor: pointer; border: 0.5px solid transparent; transition: color .1s, background .1s, border-color .1s; }
        .tb-btn:hover { color: var(--text-primary); background: var(--bg-hover); border-color: var(--border); }

        /* ── Main layout ── */
        #main { display: flex; flex: 1; overflow: hidden; }

        /* ── Sidebar ── */
        #sidebar { width: 44px; background: var(--bg-sidebar); border-right: 0.5px solid var(--border-light); display: flex; flex-direction: column; align-items: center; padding: 8px 0; gap: 2px; flex-shrink: 0; }
        .sb-btn { width: 30px; height: 30px; display: flex; align-items: center; justify-content: center; border-radius: var(--radius-sm); color: var(--text-muted); cursor: pointer; transition: color .1s, background .1s; }
        .sb-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
        .sb-btn.active { color: var(--accent-blue); background: rgba(96,165,250,.08); }
        .sb-sep { width: 20px; height: 0.5px; background: var(--border); margin: 4px 0; }
        .sb-spacer { flex: 1; }

        /* ── File Explorer ── */
        #explorer-panel {
            width: var(--explorer-w);
            background: var(--bg-explorer);
            border-right: 0.5px solid var(--border);
            display: flex;
            flex-direction: column;
            flex-shrink: 0;
            overflow: hidden;
            transition: width .2s ease;
        }
        #explorer-panel.hidden { width: 0; }

        .explorer-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 12px;
            height: 32px;
            border-bottom: 0.5px solid var(--border-light);
            flex-shrink: 0;
        }
        .explorer-title { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-muted); }
        .explorer-actions { display: flex; gap: 2px; }
        .exp-btn { width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; border-radius: 3px; color: var(--text-dim); cursor: pointer; transition: color .1s, background .1s; }
        .exp-btn:hover { color: var(--text-primary); background: var(--bg-hover); }

        .explorer-path {
            padding: 5px 10px;
            font-size: 10px;
            color: var(--text-muted);
            border-bottom: 0.5px solid var(--border-light);
            display: flex;
            align-items: center;
            gap: 5px;
            flex-shrink: 0;
            overflow: hidden;
        }
        .explorer-path span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }

        #explorer-tree {
            flex: 1;
            overflow-y: auto;
            overflow-x: hidden;
            padding: 4px 0;
            scrollbar-width: thin;
            scrollbar-color: rgba(255,255,255,.07) transparent;
        }
        #explorer-tree::-webkit-scrollbar { width: 4px; }
        #explorer-tree::-webkit-scrollbar-thumb { background: rgba(255,255,255,.07); border-radius: 2px; }

        .tree-item {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 3px 10px;
            cursor: pointer;
            font-size: 12px;
            color: var(--text-muted);
            transition: background .08s, color .08s;
            white-space: nowrap;
            overflow: hidden;
        }
        .tree-item:hover { background: var(--bg-hover); color: var(--text-primary); }
        .tree-item.active-file { background: rgba(96,165,250,.1); color: var(--accent-blue); }
        .tree-item .tree-indent { flex-shrink: 0; }
        .tree-item .tree-arrow { width: 12px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; color: var(--text-dim); transition: transform .15s; }
        .tree-item .tree-arrow.open { transform: rotate(90deg); }
        .tree-item .tree-icon { flex-shrink: 0; }
        .tree-item .tree-name { overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 0; }

        .tree-loader { padding: 12px 10px; font-size: 11px; color: var(--text-dim); }

        /* ── Content area ── */
        #content-area { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

        /* ── Terminal container ── */
        #terminal-wrap { flex: 1; display: flex; flex-direction: column; background: var(--bg-terminal); overflow: hidden; }
        #terminal-header { display: flex; align-items: center; justify-content: space-between; padding: 0 14px; height: 32px; border-bottom: 0.5px solid var(--border-light); flex-shrink: 0; }
        .conn-badge { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--text-muted); }
        .conn-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-dim); transition: background .3s; }
        .conn-dot.connected { background: var(--accent-green); box-shadow: 0 0 4px rgba(74,222,128,.4); }
        .conn-label { transition: color .3s; }
        .conn-label.connected { color: var(--accent-green); }
        .header-actions { display: flex; gap: 4px; }
        .hdr-btn { width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; border-radius: 4px; color: var(--text-dim); cursor: pointer; transition: color .1s, background .1s; }
        .hdr-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
        #terminal-box { flex: 1; padding: 10px 12px; overflow: hidden; }
        .xterm { height: 100% !important; }
        .xterm-viewport { background: var(--bg-terminal) !important; }
        .xterm-screen { background: var(--bg-terminal) !important; }

        /* ── Editor container ── */
        #editor-wrap { flex: 1; display: flex; flex-direction: column; background: var(--bg-editor); overflow: hidden; display: none; }
        #editor-wrap.visible { display: flex; }
        #editor-header { display: flex; align-items: center; justify-content: space-between; padding: 0 14px; height: 32px; border-bottom: 0.5px solid var(--border-light); flex-shrink: 0; background: #0a0a0a; }
        .editor-file-info { display: flex; align-items: center; gap: 7px; font-size: 11px; color: var(--text-muted); min-width: 0; }
        .editor-file-info .file-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .editor-file-info .unsaved-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent-yellow); display: none; flex-shrink: 0; }
        .editor-file-info .unsaved-dot.visible { display: block; }
        #editor-actions { display: flex; gap: 4px; }
        .editor-save-btn { display: flex; align-items: center; gap: 5px; padding: 0 10px; height: 22px; border-radius: 4px; font-size: 11px; background: rgba(96,165,250,.15); color: var(--accent-blue); border: 0.5px solid rgba(96,165,250,.25); cursor: pointer; transition: background .1s, border-color .1s; }
        .editor-save-btn:hover { background: rgba(96,165,250,.25); border-color: rgba(96,165,250,.4); }
        .editor-save-toast { font-size: 11px; color: var(--accent-green); opacity: 0; transition: opacity .3s; padding: 0 8px; }
        .editor-save-toast.show { opacity: 1; }
        #monaco-container { flex: 1; overflow: hidden; }

        /* ── Status bar ── */
        #statusbar { display: flex; align-items: center; justify-content: space-between; padding: 0 14px; height: 22px; background: var(--bg-statusbar); border-top: 0.5px solid var(--border-light); flex-shrink: 0; font-family: 'Menlo','Monaco','Courier New',monospace; font-size: 10px; color: var(--text-muted); }
        .sb-left, .sb-right { display: flex; gap: 14px; align-items: center; }
        .sb-item { display: flex; align-items: center; gap: 4px; }
        .sb-indicator { width: 5px; height: 5px; border-radius: 50%; background: var(--text-dim); transition: background .3s; }
        .sb-indicator.connected { background: var(--accent-green); }
        #sb-cursor { display: none; }
        #sb-cursor.visible { display: flex; }
    </style>
</head>
<body>
<div id="app">

    <!-- Title bar -->
    <div id="titlebar">
        <div class="dots">
            <div class="dot dot-r"></div>
            <div class="dot dot-y"></div>
            <div class="dot dot-g"></div>
        </div>
        <div id="tabs">
            <div class="tab active" data-tab="terminal">
                <div class="tab-icon terminal"></div>
                <span id="tab-label">e2b · bash</span>
                <div class="tab-close" data-close="terminal">×</div>
            </div>
            <div class="tab-add" title="New tab" id="tab-add-btn">+</div>
        </div>
        <div class="tb-actions">
            <div class="tb-btn" title="Split">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="1" y="1" width="5" height="12" rx="1.5" stroke="currentColor" stroke-width="1.2"/><rect x="8" y="1" width="5" height="12" rx="1.5" stroke="currentColor" stroke-width="1.2"/></svg>
            </div>
            <div class="tb-btn" title="Maximise">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="1.5" y="1.5" width="11" height="11" rx="2" stroke="currentColor" stroke-width="1.2"/></svg>
            </div>
        </div>
    </div>

    <!-- Main area -->
    <div id="main">

        <!-- Sidebar -->
        <div id="sidebar">
            <div class="sb-btn active" title="Terminal" id="sb-terminal-btn">
                <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M2 4l4 3.5L2 11" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/><path d="M8 11h5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
            </div>
            <div class="sb-btn" title="Files" id="sb-files-btn">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 3.5A1.5 1.5 0 013.5 2h3L8 4h2.5A1.5 1.5 0 0112 5.5v5A1.5 1.5 0 0110.5 12h-7A1.5 1.5 0 012 10.5v-7z" stroke="currentColor" stroke-width="1.2"/></svg>
            </div>
            <div class="sb-btn" title="Search">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="6" cy="6" r="4" stroke="currentColor" stroke-width="1.2"/><path d="M9.5 9.5l2.5 2.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
            </div>
            <div class="sb-sep"></div>
            <div class="sb-btn" title="History">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.2"/><path d="M7 4.5V7l1.5 1.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </div>
            <div class="sb-spacer"></div>
            <div class="sb-btn" title="Settings">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="2" stroke="currentColor" stroke-width="1.2"/><path d="M7 1v1.5M7 11.5V13M1 7h1.5M11.5 7H13M2.636 2.636l1.06 1.06M10.304 10.304l1.06 1.06M2.636 11.364l1.06-1.06M10.304 3.696l1.06-1.06" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
            </div>
        </div>

        <!-- File Explorer Panel -->
        <div id="explorer-panel" class="hidden">
            <div class="explorer-header">
                <span class="explorer-title">Explorer</span>
                <div class="explorer-actions">
                    <div class="exp-btn" title="Go up" id="exp-up-btn">
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M6 9V3M3 6l3-3 3 3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
                    </div>
                    <div class="exp-btn" title="Refresh" id="exp-refresh-btn">
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M10 6a4 4 0 11-1-2.65" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/><path d="M9 2v2.5H6.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
                    </div>
                </div>
            </div>
            <div class="explorer-path">
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M1 3A1 1 0 012 2h2l1 1.5h3A1 1 0 019 4.5v3a1 1 0 01-1 1H2a1 1 0 01-1-1V3z" stroke="currentColor" stroke-width="1"/></svg>
                <span id="explorer-current-path">/</span>
            </div>
            <div id="explorer-tree"><div class="tree-loader">Loading…</div></div>
        </div>

        <!-- Content area (terminal + editor) -->
        <div id="content-area">

            <!-- Terminal panel -->
            <div id="terminal-wrap">
                <div id="terminal-header">
                    <div class="conn-badge">
                        <div class="conn-dot" id="conn-dot"></div>
                        <span class="conn-label" id="conn-label">Connecting…</span>
                    </div>
                    <div class="header-actions">
                        <div class="hdr-btn" title="Clear" id="btn-clear">
                            <svg width="13" height="13" viewBox="0 0 13 13" fill="none"><path d="M2 11L6 4m0 0l4 7M6 4V2" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
                        </div>
                        <div class="hdr-btn" title="Copy selection" id="btn-copy">
                            <svg width="13" height="13" viewBox="0 0 13 13" fill="none"><rect x="4" y="4" width="7.5" height="7.5" rx="1.5" stroke="currentColor" stroke-width="1.2"/><path d="M9 4V2.5A1.5 1.5 0 007.5 1H2.5A1.5 1.5 0 001 2.5V8A1.5 1.5 0 002.5 9.5H4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
                        </div>
                    </div>
                </div>
                <div id="terminal-box"></div>
            </div>

            <!-- Editor panel -->
            <div id="editor-wrap">
                <div id="editor-header">
                    <div class="editor-file-info">
                        <div class="unsaved-dot" id="editor-unsaved-dot"></div>
                        <span class="file-path" id="editor-file-path">—</span>
                    </div>
                    <div id="editor-actions">
                        <span class="editor-save-toast" id="save-toast">✓ Saved</span>
                        <div class="editor-save-btn" id="btn-save">
                            <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2 10h8M3 2v4h6V2M4 8.5h4" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/></svg>
                            Save
                        </div>
                    </div>
                </div>
                <div id="monaco-container"></div>
            </div>

        </div>
    </div>

    <!-- Status bar -->
    <div id="statusbar">
        <div class="sb-left">
            <div class="sb-item">
                <div class="sb-indicator" id="sb-indicator"></div>
                <span id="sb-status">Disconnected</span>
            </div>
            <div class="sb-item" id="sb-pid" style="display:none">e2b sandbox · bash</div>
        </div>
        <div class="sb-right">
            <div class="sb-item sb-cursor" id="sb-cursor">Ln —, Col —</div>
            <div class="sb-item" id="sb-size">—×—</div>
            <div class="sb-item">UTF-8</div>
            <div class="sb-item">xterm-256color</div>
            <div class="sb-item">E2B Cloud</div>
        </div>
    </div>

</div>

<script src="https://cdn.jsdelivr.net/npm/xterm/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit/lib/xterm-addon-fit.js"></script>
<script>
// Load Monaco
(function() {
    var loaderScript = document.createElement('script');
    loaderScript.src = 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.44.0/min/vs/loader.min.js';
    loaderScript.onload = function() {
        require.config({ paths: { vs: 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.44.0/min/vs' } });
        require(['vs/editor/editor.main'], function() { window._monacoReady = true; window.dispatchEvent(new Event('monacoready')); });
    };
    document.head.appendChild(loaderScript);
})();
</script>
<script>
(function () {
    // ── State ──────────────────────────────────────────────
    var state = {
        explorerOpen: false,
        activeTab: 'terminal',
        explorerPath: '/',
        openFiles: {},
        activeFile: null,
        expandedDirs: {},
        monacoEditor: null,
        monacoReady: false,
    };

    // ── Monaco init ─────────────────────────────────────────
    function initMonaco() {
        if (state.monacoEditor) return;
        var container = document.getElementById('monaco-container');
        state.monacoEditor = monaco.editor.create(container, {
            value: '',
            language: 'plaintext',
            theme: 'vs-dark',
            fontSize: 13,
            fontFamily: 'Menlo, Monaco, "Courier New", monospace',
            lineHeight: 1.6,
            minimap: { enabled: true },
            scrollBeyondLastLine: false,
            renderWhitespace: 'selection',
            smoothScrolling: true,
            cursorBlinking: 'smooth',
            tabSize: 4,
            wordWrap: 'off',
            automaticLayout: true,
            scrollbar: { vertical: 'auto', horizontal: 'auto' },
        });

        monaco.editor.defineTheme('shell-dark', {
            base: 'vs-dark',
            inherit: true,
            rules: [],
            colors: {
                'editor.background': '#000000',
                'editorGutter.background': '#000000',
                'editor.lineHighlightBackground': '#0d0d0d',
                'editorLineNumber.foreground': '#333333',
                'editorLineNumber.activeForeground': '#555555',
                'editor.selectionBackground': '#1a3a5c',
                'editorWidget.background': '#0a0a0a',
                'editorSuggestWidget.background': '#0a0a0a',
                'editorSuggestWidget.border': '#222222',
                'minimap.background': '#000000',
                'scrollbar.shadow': '#000000',
                'scrollbarSlider.background': '#1a1a1a',
                'scrollbarSlider.hoverBackground': '#2a2a2a',
                'scrollbarSlider.activeBackground': '#3a3a3a',
            }
        });
        monaco.editor.setTheme('shell-dark');

        state.monacoEditor.onDidChangeCursorPosition(function(e) {
            var sbCursor = document.getElementById('sb-cursor');
            sbCursor.textContent = 'Ln ' + e.position.lineNumber + ', Col ' + e.position.column;
            sbCursor.classList.add('visible');
        });

        state.monacoEditor.onDidChangeModelContent(function() {
            if (state.activeFile) {
                var f = state.openFiles[state.activeFile];
                if (f) {
                    var cur = state.monacoEditor.getValue();
                    f.unsaved = cur !== f.savedContent;
                    updateUnsavedIndicator();
                }
            }
        });
    }

    window.addEventListener('monacoready', function() {
        state.monacoReady = true;
        if (state._pendingFile) { openFileInEditor(state._pendingFile); state._pendingFile = null; }
    });

    // ── Terminal setup ──────────────────────────────────────
    var term = new Terminal({
        cursorBlink: true,
        fontSize: 13,
        fontFamily: 'Menlo, Monaco, "Courier New", monospace',
        lineHeight: 1.4,
        theme: {
            background: '#141414', foreground: '#d4d4d4', cursor: '#d4d4d4', cursorAccent: '#141414',
            selectionBackground: 'rgba(255,255,255,0.15)',
            black: '#1e1e1e', brightBlack: '#666666',
            red: '#f87171', brightRed: '#fca5a5',
            green: '#4ade80', brightGreen: '#86efac',
            yellow: '#fbbf24', brightYellow: '#fcd34d',
            blue: '#60a5fa', brightBlue: '#93c5fd',
            magenta: '#a78bfa', brightMagenta: '#c4b5fd',
            cyan: '#34d399', brightCyan: '#6ee7b7',
            white: '#d4d4d4', brightWhite: '#f5f5f5',
        }
    });
    var fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById('terminal-box'));
    fitAddon.fit();

    // ── UI refs ─────────────────────────────────────────────
    var connDot    = document.getElementById('conn-dot');
    var connLabel  = document.getElementById('conn-label');
    var sbIndicator = document.getElementById('sb-indicator');
    var sbStatus   = document.getElementById('sb-status');
    var sbSize     = document.getElementById('sb-size');
    var tabLabel   = document.getElementById('tab-label');

    function setConnected(on) {
        if (on) {
            connDot.classList.add('connected'); connLabel.classList.add('connected');
            connLabel.textContent = 'Connected · E2B';
            sbIndicator.classList.add('connected'); sbStatus.textContent = 'e2b sandbox · bash · running';
        } else {
            connDot.classList.remove('connected'); connLabel.classList.remove('connected');
            connLabel.textContent = 'Disconnected';
            sbIndicator.classList.remove('connected'); sbStatus.textContent = 'Disconnected';
        }
    }
    function updateSize() { sbSize.textContent = term.cols + '×' + term.rows; }

    // ── WebSocket ────────────────────────────────────────────
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var socket = new WebSocket(proto + '//' + location.host + '/ws');
    socket.binaryType = 'arraybuffer';

    socket.onopen = function () {
        setConnected(true); updateSize();
        setTimeout(function () { sendResize(term.cols, term.rows); }, 50);
    };
    socket.onmessage = function (event) {
        if (event.data instanceof ArrayBuffer) { term.write(new Uint8Array(event.data)); }
        else { term.write(event.data); }
    };
    socket.onclose = function () {
        setConnected(false);
        term.write('\r\n\x1b[31m[E2B session closed]\x1b[0m\r\n');
        tabLabel.textContent = 'e2b · bash (closed)';
    };
    socket.onerror = function () { term.write('\r\n\x1b[31m[WebSocket error]\x1b[0m\r\n'); };

    function sendResize(cols, rows) {
        if (socket.readyState !== WebSocket.OPEN) return;
        // 4-byte little-endian: cols(u16) rows(u16)
        var buf = new Uint8Array(4);
        var view = new DataView(buf.buffer);
        view.setUint16(0, cols, true);
        view.setUint16(2, rows, true);
        socket.send(buf);
    }

    term.onData(function (data) {
        if (socket.readyState === WebSocket.OPEN)
            socket.send(new TextEncoder().encode(data));
    });
    term.onResize(function (s) { sendResize(s.cols, s.rows); updateSize(); });
    window.addEventListener('resize', function () { fitAddon.fit(); });

    document.getElementById('btn-clear').addEventListener('click', function () { term.clear(); });
    document.getElementById('btn-copy').addEventListener('click', function () {
        var sel = term.getSelection();
        if (sel) navigator.clipboard.writeText(sel).catch(function(){});
    });

    // ── Tab management ──────────────────────────────────────
    function switchToTerminal() {
        state.activeTab = 'terminal';
        state.activeFile = null;
        document.getElementById('terminal-wrap').style.display = 'flex';
        document.getElementById('editor-wrap').classList.remove('visible');
        document.getElementById('sb-cursor').classList.remove('visible');
        highlightTab('terminal');
        setTimeout(function() { fitAddon.fit(); }, 50);
    }

    function switchToFile(filePath) {
        state.activeTab = filePath;
        state.activeFile = filePath;
        document.getElementById('terminal-wrap').style.display = 'none';
        document.getElementById('editor-wrap').classList.add('visible');
        highlightTab(filePath);
        if (!state.monacoReady) { state._pendingFile = filePath; return; }
        openFileInEditor(filePath);
    }

    function openFileInEditor(filePath) {
        initMonaco();
        var f = state.openFiles[filePath];
        if (!f) return;

        var ext = filePath.split('.').pop().toLowerCase();
        var langMap = {
            js: 'javascript', ts: 'typescript', jsx: 'javascript', tsx: 'typescript',
            py: 'python', rb: 'ruby', go: 'go', rs: 'rust', c: 'c', cpp: 'cpp',
            h: 'c', cs: 'csharp', java: 'java', kt: 'kotlin', swift: 'swift',
            html: 'html', htm: 'html', css: 'css', scss: 'scss', less: 'less',
            json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'ini', xml: 'xml',
            md: 'markdown', sh: 'shell', bash: 'shell', zsh: 'shell',
            sql: 'sql', php: 'php', r: 'r', lua: 'lua', dart: 'dart',
            tf: 'hcl', env: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini',
            txt: 'plaintext', log: 'plaintext',
        };
        var lang = langMap[ext] || 'plaintext';

        if (f.monacoModel) {
            monaco.editor.setModelLanguage(f.monacoModel, lang);
            state.monacoEditor.setModel(f.monacoModel);
        } else {
            var model = monaco.editor.createModel(f.content, lang);
            f.monacoModel = model;
            state.monacoEditor.setModel(model);
        }

        document.getElementById('editor-file-path').textContent = filePath;
        updateUnsavedIndicator();
    }

    function highlightTab(id) {
        document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
        var el = document.querySelector('.tab[data-tab="' + CSS.escape(id) + '"]');
        if (el) el.classList.add('active');
    }

    function addEditorTab(filePath) {
        var existing = document.querySelector('.tab[data-tab="' + CSS.escape(filePath) + '"]');
        if (existing) { switchToFile(filePath); return; }
        var name = filePath.split('/').pop();
        var tab = document.createElement('div');
        tab.className = 'tab';
        tab.setAttribute('data-tab', filePath);
        tab.innerHTML = '<div class="tab-icon file"></div><span>' + name + '</span><div class="tab-close" data-close="' + filePath + '">×</div>';
        tab.addEventListener('click', function(e) {
            if (e.target.classList.contains('tab-close')) return;
            switchToFile(filePath);
        });
        tab.querySelector('.tab-close').addEventListener('click', function(e) {
            e.stopPropagation(); closeFileTab(filePath);
        });
        var addBtn = document.getElementById('tab-add-btn');
        document.getElementById('tabs').insertBefore(tab, addBtn);
        switchToFile(filePath);
    }

    function closeFileTab(filePath) {
        var tab = document.querySelector('.tab[data-tab="' + CSS.escape(filePath) + '"]');
        if (tab) tab.remove();
        if (state.openFiles[filePath] && state.openFiles[filePath].monacoModel) {
            state.openFiles[filePath].monacoModel.dispose();
        }
        delete state.openFiles[filePath];
        if (state.activeFile === filePath) switchToTerminal();
    }

    function updateUnsavedIndicator() {
        var dot = document.getElementById('editor-unsaved-dot');
        var f = state.activeFile && state.openFiles[state.activeFile];
        if (f && f.unsaved) {
            dot.classList.add('visible');
            var tab = document.querySelector('.tab[data-tab="' + CSS.escape(state.activeFile) + '"] .tab-icon');
            if (tab) { tab.className = 'tab-icon unsaved'; }
        } else {
            dot.classList.remove('visible');
            if (state.activeFile) {
                var tab2 = document.querySelector('.tab[data-tab="' + CSS.escape(state.activeFile) + '"] .tab-icon');
                if (tab2) { tab2.className = 'tab-icon file'; }
            }
        }
    }

    document.querySelector('.tab[data-tab="terminal"]').addEventListener('click', function(e) {
        if (e.target.classList.contains('tab-close')) return;
        switchToTerminal();
    });

    // ── File Save ───────────────────────────────────────────
    document.getElementById('btn-save').addEventListener('click', saveCurrentFile);

    function saveCurrentFile() {
        if (!state.activeFile || !state.monacoEditor) return;
        var content = state.monacoEditor.getValue();
        fetch('/api/file', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: state.activeFile, content: content })
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data.ok) {
                var f = state.openFiles[state.activeFile];
                if (f) { f.savedContent = content; f.unsaved = false; }
                updateUnsavedIndicator();
                var toast = document.getElementById('save-toast');
                toast.classList.add('show');
                setTimeout(function() { toast.classList.remove('show'); }, 1800);
            }
        }).catch(function(e) { console.error('Save error:', e); });
    }

    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            if (state.activeFile) { e.preventDefault(); saveCurrentFile(); }
        }
    });

    // ── Sidebar buttons ─────────────────────────────────────
    var sbFilesBtn = document.getElementById('sb-files-btn');
    var sbTerminalBtn = document.getElementById('sb-terminal-btn');

    sbFilesBtn.addEventListener('click', function() {
        state.explorerOpen = !state.explorerOpen;
        var panel = document.getElementById('explorer-panel');
        if (state.explorerOpen) {
            panel.classList.remove('hidden');
            sbFilesBtn.classList.add('active');
            if (!state._explorerLoaded) { loadExplorer(state.explorerPath); state._explorerLoaded = true; }
        } else {
            panel.classList.add('hidden');
            sbFilesBtn.classList.remove('active');
        }
    });

    sbTerminalBtn.addEventListener('click', function() { switchToTerminal(); });

    // ── Explorer logic ──────────────────────────────────────
    document.getElementById('exp-refresh-btn').addEventListener('click', function() {
        loadExplorer(state.explorerPath);
    });
    document.getElementById('exp-up-btn').addEventListener('click', function() {
        var parts = state.explorerPath.replace(/\/$/, '').split('/');
        parts.pop();
        var parent = parts.join('/') || '/';
        state.expandedDirs = {};
        loadExplorer(parent);
    });

    function loadExplorer(path) {
        state.explorerPath = path;
        document.getElementById('explorer-current-path').textContent = path;
        var tree = document.getElementById('explorer-tree');
        tree.innerHTML = '<div class="tree-loader">Loading…</div>';
        fetch('/api/files?path=' + encodeURIComponent(path))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error) { tree.innerHTML = '<div class="tree-loader" style="color:#f87171">' + data.error + '</div>'; return; }
                renderTree(tree, data.entries, 0);
            })
            .catch(function() { tree.innerHTML = '<div class="tree-loader" style="color:#f87171">Error loading</div>'; });
    }

    function renderTree(container, entries, depth) {
        container.innerHTML = '';
        if (entries.length === 0) { container.innerHTML = '<div class="tree-loader">Empty</div>'; return; }
        entries.forEach(function(entry) {
            var item = createTreeItem(entry, depth);
            container.appendChild(item);
            if (entry.is_dir && state.expandedDirs[entry.path]) {
                var subContainer = document.createElement('div');
                subContainer.id = 'subtree-' + btoa(entry.path).replace(/[^a-zA-Z0-9]/g, '');
                container.appendChild(subContainer);
                loadSubtree(entry.path, subContainer, depth + 1);
            }
        });
    }

    function createTreeItem(entry, depth) {
        var item = document.createElement('div');
        item.className = 'tree-item';
        if (!entry.is_dir && state.activeFile === entry.path) item.classList.add('active-file');

        var indent = document.createElement('div');
        indent.className = 'tree-indent';
        indent.style.width = (depth * 14) + 'px';
        indent.style.flexShrink = '0';

        var arrow = document.createElement('div');
        arrow.className = 'tree-arrow';
        if (!entry.is_dir) arrow.style.visibility = 'hidden';
        else if (state.expandedDirs[entry.path]) arrow.classList.add('open');
        arrow.innerHTML = '<svg width="8" height="8" viewBox="0 0 8 8" fill="none"><path d="M2 2l4 2-4 2" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

        var icon = document.createElement('div');
        icon.className = 'tree-icon';
        icon.innerHTML = entry.is_dir ? folderIcon(state.expandedDirs[entry.path]) : fileIcon(entry.name);

        var name = document.createElement('div');
        name.className = 'tree-name';
        name.textContent = entry.name;

        item.appendChild(indent); item.appendChild(arrow); item.appendChild(icon); item.appendChild(name);

        if (entry.is_dir) {
            item.addEventListener('click', function() {
                var subId = 'subtree-' + btoa(entry.path).replace(/[^a-zA-Z0-9]/g, '');
                if (state.expandedDirs[entry.path]) {
                    delete state.expandedDirs[entry.path];
                    arrow.classList.remove('open');
                    icon.innerHTML = folderIcon(false);
                    var sub = document.getElementById(subId);
                    if (sub) sub.remove();
                } else {
                    state.expandedDirs[entry.path] = true;
                    arrow.classList.add('open');
                    icon.innerHTML = folderIcon(true);
                    var sub2 = document.createElement('div');
                    sub2.id = subId;
                    item.parentNode.insertBefore(sub2, item.nextSibling);
                    loadSubtree(entry.path, sub2, depth + 1);
                }
            });
        } else {
            item.addEventListener('click', function() {
                document.querySelectorAll('.tree-item.active-file').forEach(function(i) { i.classList.remove('active-file'); });
                item.classList.add('active-file');
                openFile(entry.path);
            });
        }
        return item;
    }

    function loadSubtree(path, container, depth) {
        container.innerHTML = '<div class="tree-item" style="padding-left:' + (depth*14+22) + 'px;font-size:11px;color:var(--text-dim)">Loading…</div>';
        fetch('/api/files?path=' + encodeURIComponent(path))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error) { container.innerHTML = ''; return; }
                container.innerHTML = '';
                data.entries.forEach(function(entry) {
                    var item = createTreeItem(entry, depth);
                    container.appendChild(item);
                    if (entry.is_dir && state.expandedDirs[entry.path]) {
                        var sub = document.createElement('div');
                        sub.id = 'subtree-' + btoa(entry.path).replace(/[^a-zA-Z0-9]/g, '');
                        container.appendChild(sub);
                        loadSubtree(entry.path, sub, depth + 1);
                    }
                });
            })
            .catch(function() { container.innerHTML = ''; });
    }

    function openFile(filePath) {
        if (state.openFiles[filePath]) { addEditorTab(filePath); return; }
        fetch('/api/file?path=' + encodeURIComponent(filePath))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error) { term.write('\r\n\x1b[31m[Error opening file: ' + data.error + ']\x1b[0m\r\n'); return; }
                state.openFiles[filePath] = { content: data.content, savedContent: data.content, unsaved: false, monacoModel: null };
                addEditorTab(filePath);
            })
            .catch(function(e) { console.error('Error opening file:', e); });
    }

    // ── Icon helpers ─────────────────────────────────────────
    function folderIcon(open) {
        var c = open ? '#fbbf24' : '#6b6b6b';
        return '<svg width="13" height="13" viewBox="0 0 13 13" fill="none"><path d="M1.5 3.5A1 1 0 012.5 2.5h2.3l1 1.5h4.7a1 1 0 011 1v4a1 1 0 01-1 1h-8a1 1 0 01-1-1v-5.5z" fill="' + c + '" opacity="0.8"/></svg>';
    }

    function fileIcon(name) {
        var ext = name.split('.').pop().toLowerCase();
        var colors = {
            js: '#fbbf24', ts: '#60a5fa', jsx: '#fbbf24', tsx: '#60a5fa',
            py: '#4ade80', rb: '#f87171', go: '#34d399', rs: '#f97316',
            html: '#fb923c', css: '#818cf8', scss: '#f472b6',
            json: '#fbbf24', md: '#94a3b8', sh: '#4ade80', bash: '#4ade80',
            sql: '#60a5fa', php: '#a78bfa', yaml: '#fbbf24', yml: '#fbbf24',
            c: '#60a5fa', cpp: '#60a5fa', h: '#60a5fa',
        };
        var c = colors[ext] || '#6b6b6b';
        return '<svg width="11" height="13" viewBox="0 0 11 13" fill="none"><path d="M1 1.5A.5.5 0 011.5 1H7l3 3v7.5a.5.5 0 01-.5.5h-8a.5.5 0 01-.5-.5v-10z" stroke="' + c + '" stroke-width="1" fill="none" opacity="0.7"/><path d="M7 1v3h3" stroke="' + c + '" stroke-width="1" opacity="0.5"/></svg>';
    }

})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def get_terminal():
    return HTML_TEMPLATE


# ── E2B PTY WebSocket ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    sbx = get_sandbox()

    loop = asyncio.get_event_loop()
    pty_handle = None
    pty_pid: int | None = None

    # Callback: E2B PTY → browser
    async def on_data(data: bytes) -> None:
        try:
            await websocket.send_bytes(data)
        except Exception:
            pass

    # Sync wrapper required by E2B SDK (it calls on_data from a thread)
    def on_data_sync(output) -> None:
        raw: bytes = output.data if hasattr(output, "data") else bytes(output)
        asyncio.run_coroutine_threadsafe(on_data(raw), loop)

    try:
        # Create the PTY inside the E2B sandbox
        pty_handle = await sbx.pty.create(
            size=PtySize(cols=220, rows=50),
            on_data=on_data_sync,
            timeout=0,          # keep alive indefinitely
            user="user",
        )
        pty_pid = pty_handle.pid

        # Main receive loop: browser → E2B PTY
        while True:
            message = await websocket.receive()

            if "bytes" in message:
                data: bytes = message["bytes"]
                if len(data) == 4:
                    # Resize packet: cols(u16 LE) rows(u16 LE)
                    cols = struct.unpack_from("<H", data, 0)[0]
                    rows = struct.unpack_from("<H", data, 2)[0]
                    await sbx.pty.resize(pty_pid, PtySize(cols=cols, rows=rows))
                else:
                    await sbx.pty.send_stdin(pty_pid, data)

            elif "text" in message:
                await sbx.pty.send_stdin(pty_pid, message["text"].encode())

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[WS] Error: {exc}")
    finally:
        if pty_pid is not None:
            try:
                await sbx.pty.kill(pty_pid)
            except Exception:
                pass


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
