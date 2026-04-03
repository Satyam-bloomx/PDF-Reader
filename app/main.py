"""
FastAPI backend for the PDF Beautifier tool.

Endpoints:
  POST /upload   — upload a PDF, returns a job_id
  GET  /status/{job_id} — check processing status
  GET  /download/{job_id} — download the beautified PDF
"""

import os
import uuid
import time
import shutil
import asyncio
import traceback
import tempfile
import threading
from pathlib import Path
from contextlib import asynccontextmanager

_recolor_lock = threading.Lock()

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
import io

from .recolor import _recolor_array, _set_palette

BASE_DIR = Path(__file__).parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

async def cleanup_jobs():
    while True:
        try:
            now = time.time()
            
            # 1. Clear the dictionary for memory safety
            keys_to_delete = []
            for jid, job in list(jobs.items()):
                if now - job.get("timestamp", now) > 3600:
                    keys_to_delete.append(jid)
            for jid in keys_to_delete:
                del jobs[jid]

            # 2. Sweep the actual folders for orphaned files
            for directory in [UPLOAD_DIR, OUTPUT_DIR]:
                if not directory.exists(): continue
                for item in directory.iterdir():
                    if item.is_file():
                        # If file was modified more than 1 hour ago, delete it
                        if now - item.stat().st_mtime > 3600:
                            try: 
                                item.unlink()
                                print(f"[cleanup] Deleted orphan: {item.name}")
                            except Exception as e: 
                                print(f"[cleanup] Failed to delete {item.name}: {e}")

        except Exception as e:
            print(f"[cleanup] error: {e}")
        await asyncio.sleep(600)  # run every 10 min

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(cleanup_jobs())
    yield
    task.cancel()

app = FastAPI(title="PDF Beautifier", lifespan=lifespan)



jobs: dict = {}


def hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return (r, g, b)


@app.get("/", response_class=HTMLResponse)
async def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PDF Beautifier &#x2014; Celestial</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r134/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.2/dist/confetti.browser.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=Space+Grotesk:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; user-select: none; -webkit-user-select: none; -webkit-tap-highlight-color: transparent; }
    :root {
      --gold: #b8924a; --gold-lt: #d4aa6a; --gold-dim: rgba(184,146,74,0.28);
      --deep: #1c1810; --border: rgba(184,146,74,0.18);
      --text: rgba(230,215,188,0.9); --muted: rgba(200,182,148,0.55);
      --panel: rgba(28,22,14,0.82);
    }
    html, body { height: 100%; overflow: hidden; background: var(--deep); color: var(--text); font-family: 'Space Grotesk', sans-serif; }
    #bg-canvas { position: fixed; inset: 0; z-index: 0; width: 100% !important; height: 100% !important; }

    #app { position: relative; z-index: 1; height: 100vh; display: flex; flex-direction: column; padding: 0 32px; }

    header {
      flex-shrink: 0; display: flex; flex-direction: column; align-items: center;
      justify-content: center; padding: 10px 0 8px; gap: 2px;
      border-bottom: 1px solid var(--border);
    }
    .eyebrow { font-size: 0.58rem; letter-spacing: 0.32em; text-transform: uppercase; color: var(--gold); opacity: 0.8; }
    h1 {
      font-family: 'Cormorant Garamond', serif;
      font-size: clamp(1.4rem, 2.4vh, 1.85rem); font-weight: 600;
      letter-spacing: 0.18em; text-transform: uppercase;
      background: linear-gradient(135deg, #e8d4a0 0%, #b8924a 45%, #d4aa6a 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    }
    .subtitle { font-size: 0.6rem; color: var(--muted); letter-spacing: 0.18em; text-transform: uppercase; }

    main { flex: 1; display: flex; align-items: center; justify-content: center; gap: 44px; min-height: 0; padding: 10px 0; }

    /* Wheel */
    .wheel-col { flex-shrink: 0; position: relative; display: flex; align-items: center; justify-content: center; }
    #zodiac-svg {
      width: min(calc(100vh - 120px), 46vw);
      height: min(calc(100vh - 120px), 46vw);
      filter: drop-shadow(0 0 30px rgba(184,146,74,0.08));
      overflow: visible;
    }

    /* Drop zone */
    #drop-zone {
      position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
      border-radius: 50%; cursor: pointer;
      display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px;
      border: 1px solid rgba(184,146,74,0.25); background: rgba(22,17,10,0.9);
      transition: border-color 0.3s, background 0.3s; text-align: center; overflow: hidden;
    }
    #drop-zone:hover { border-color: rgba(184,146,74,0.55); background: rgba(184,146,74,0.06); }
    #drop-zone.dragover { border-color: var(--gold); background: rgba(184,146,74,0.1); }
    #drop-zone input { display: none; }
    .dz-moon { color: var(--gold); filter: drop-shadow(0 0 8px rgba(184,146,74,0.4)); line-height: 1; }
    .dz-lbl { font-family: 'Cormorant Garamond', serif; color: var(--muted); font-style: italic; line-height: 1.2; }
    .dz-sub { color: rgba(200,182,148,0.32); letter-spacing: 0.04em; }
    #fname { color: #e8e8e8; max-width: 88%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 4px 10px; display: none; font-size: 0.78rem; text-align: center; }
    #fname-badge { display: none; flex-direction: column; align-items: center; gap: 3px; }
    #fname-badge .fname-check { color: #7dde82; font-size: 1.5em; line-height: 1; }
    #fname-badge .fname-label { font-size: 0.58rem; color: #7dde82; text-transform: uppercase; letter-spacing: 0.14em; font-weight: 500; }
    #fname-badge .fname-name { color: #e8e4d8; font-size: 0.7rem; max-width: 90%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: center; padding: 0 6px; }
    #fname-badge .fname-reupload { font-size: 0.55rem; color: var(--muted); text-decoration: underline; cursor: pointer; margin-top: 2px; letter-spacing: 0.06em; }
    #fname-badge .fname-reupload:hover { color: var(--gold-lt); }

    /* Controls panel — frosted parchment card */
    .ctrl-col {
      flex: 0 0 290px; display: flex; flex-direction: column; gap: 11px; min-height: 0;
      background: var(--panel);
      backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
      border: 1px solid rgba(184,146,74,0.2);
      border-radius: 20px;
      padding: 18px 16px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.35), inset 0 1px 0 rgba(230,215,188,0.06);
    }

    .sign-card {
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(184,146,74,0.16); border-radius: 13px; padding: 11px 14px;
      display: flex; align-items: center; gap: 11px; min-height: 62px;
      transition: border-color 0.4s, background 0.4s;
    }
    .sign-glyph { font-size: 1.9rem; line-height: 1; color: var(--gold); flex-shrink: 0; width: 36px; text-align: center; transition: color 0.3s, filter 0.3s; }
    .sign-name { font-family: 'Cormorant Garamond', serif; font-size: 1.05rem; font-weight: 600; letter-spacing: 0.08em; color: var(--gold-lt); transition: color 0.3s; }
    .sign-meta { font-size: 0.59rem; letter-spacing: 0.15em; text-transform: uppercase; opacity: 0.65; margin-top: 2px; transition: color 0.3s; }

    .sec-ttl {
      font-size: 0.56rem; letter-spacing: 0.24em; text-transform: uppercase; color: var(--gold); opacity: 0.7;
      display: flex; align-items: center; gap: 8px;
    }
    .sec-ttl::after { content: ''; flex: 1; height: 1px; background: rgba(184,146,74,0.18); }

    .colors-wrap { display: flex; flex-direction: column; gap: 7px; }
    .color-row { display: flex; align-items: center; gap: 8px; }
    .color-lbl { font-size: 0.69rem; color: var(--muted); flex: 0 0 68px; }
    .color-grp { display: flex; align-items: center; gap: 6px; flex: 1; }
    input[type=color] { width: 30px; height: 26px; border: 1px solid rgba(184,146,74,0.2); border-radius: 6px; cursor: pointer; background: none; padding: 2px; flex-shrink: 0; }
    .hex-in { flex: 1; height: 26px; background: rgba(0,0,0,0.25); border: 1px solid rgba(184,146,74,0.18); border-radius: 6px; color: var(--text); font-size: 0.72rem; padding: 0 8px; font-family: 'Space Grotesk', monospace; text-transform: uppercase; }
    .hex-in:focus { outline: none; border-color: rgba(184,146,74,0.45); background: rgba(0,0,0,0.35); }
    .op-wrap { display: flex; flex-direction: column; gap: 4px; }
    .op-lbl { font-size: 0.69rem; color: var(--muted); display: flex; justify-content: space-between; }
    input[type=range] { width: 100%; accent-color: var(--gold); cursor: pointer; }
    #preview { border-radius: 10px; padding: 7px 14px; font-family: 'Cormorant Garamond', serif; font-size: 0.88rem; font-style: italic; border: 1px solid rgba(184,146,74,0.14); transition: all 0.35s; text-align: center; }

    .btn-main {
      background: linear-gradient(135deg, rgba(184,146,74,0.18), rgba(184,146,74,0.08));
      border: 1px solid rgba(184,146,74,0.35); color: var(--gold-lt); border-radius: 12px;
      padding: 11px; font-family: 'Cormorant Garamond', serif; font-size: 1rem; font-weight: 600;
      letter-spacing: 0.18em; cursor: pointer; transition: all 0.25s; width: 100%; text-transform: uppercase;
    }
    .btn-main:hover:not(:disabled) { background: linear-gradient(135deg, rgba(184,146,74,0.32), rgba(184,146,74,0.16)); border-color: var(--gold); box-shadow: 0 0 20px rgba(184,146,74,0.2); transform: translateY(-1px); }
    .btn-main:disabled { opacity: 0.3; cursor: not-allowed; }

    #status { font-size: 0.75rem; padding: 9px 13px; border-radius: 10px; display: none; line-height: 1.4; }
    #status.processing { background: rgba(80,60,30,0.35); color: #d4b87a; border: 1px solid rgba(184,146,74,0.25); }
    #status.done { background: rgba(30,60,35,0.4); color: #88cc90; border: 1px solid rgba(60,160,70,0.25); }
    #status.error { background: rgba(80,20,20,0.35); color: #e08080; border: 1px solid rgba(180,60,60,0.25); }

    .mode-toggle-wrap { display: flex; gap: 6px; }
    .mode-opt {
      flex: 1; display: flex; align-items: center; gap: 8px; padding: 8px 10px;
      border: 1px solid rgba(184,146,74,0.18); border-radius: 10px; cursor: pointer;
      background: rgba(0,0,0,0.2); transition: all 0.2s;
    }
    .mode-opt:hover { border-color: rgba(184,146,74,0.4); background: rgba(184,146,74,0.06); }
    .mode-opt.active { border-color: rgba(184,146,74,0.55); background: rgba(184,146,74,0.12); }
    .mode-icon { font-size: 1rem; color: var(--gold); flex-shrink: 0; }
    .mode-name { font-size: 0.68rem; font-weight: 600; color: var(--text); letter-spacing: 0.04em; }
    .mode-desc { font-size: 0.56rem; color: var(--muted); margin-top: 1px; }

    #dl-btn {
      background: linear-gradient(135deg, rgba(90,160,80,0.2), rgba(60,130,60,0.1));
      border: 1px solid rgba(90,160,80,0.32); color: #88cc90; border-radius: 12px; padding: 10px;
      font-family: 'Cormorant Garamond', serif; font-size: 0.92rem; font-weight: 600;
      letter-spacing: 0.1em; cursor: pointer; transition: all 0.25s; width: 100%; display: none;
    }
    #dl-btn:hover { background: linear-gradient(135deg, rgba(90,160,80,0.32), rgba(60,130,60,0.18)); box-shadow: 0 0 18px rgba(80,160,80,0.2); }

    #tip { position: fixed; pointer-events: none; background: rgba(22,17,10,0.96); border: 1px solid rgba(184,146,74,0.28); border-radius: 8px; padding: 5px 12px; font-size: 0.68rem; font-family: 'Cormorant Garamond', serif; font-style: italic; color: var(--gold-lt); z-index: 999; display: none; white-space: nowrap; backdrop-filter: blur(10px); }

    .seg { cursor: pointer; outline: none; -webkit-tap-highlight-color: transparent; }
    .seg:hover .sf { opacity: 0.14 !important; }
    #zodiac-svg, #zodiac-svg * { -webkit-tap-highlight-color: transparent; outline: none; }
    #drop-zone { -webkit-tap-highlight-color: transparent; outline: none; }

    @keyframes gold-pulse { 0%,100% { opacity: 0.4; } 50% { opacity: 0.85; } }
    .pulse-ring { animation: gold-pulse 2.2s ease-in-out infinite; transform-origin: 300px 300px; }

    /* ── Mobile responsive ── */
    @media (max-width: 1100px) {
      .prev-col { flex: 0 0 260px; }
      main { gap: 28px; }
    }
    @media (max-width: 860px) {
      html, body { height: auto; overflow: auto; }
      #app { height: auto; min-height: 100dvh; padding: 0 16px 24px; }
      main { flex-direction: column; gap: 20px; padding: 12px 0; align-items: center; }
      .wheel-col { width: 100%; display: flex; justify-content: center; }
      #zodiac-svg { width: min(92vw, 56vh); height: min(92vw, 56vh); }
      .ctrl-col { flex: none; width: min(92vw, 420px); padding: 16px 14px; }
      .prev-col { flex: none; width: min(92vw, 420px); min-height: 420px; align-self: auto; padding: 16px 14px; }
      header { padding: 8px 0 6px; }
      h1 { font-size: clamp(1.2rem, 5vw, 1.6rem); }
      .eyebrow { font-size: 0.52rem; letter-spacing: 0.22em; }
      .subtitle { font-size: 0.55rem; letter-spacing: 0.12em; }
      #tip { display: none !important; }
    }
    @media (max-width: 420px) {
      #zodiac-svg { width: 94vw; height: 94vw; }
      .ctrl-col, .prev-col { width: 94vw; padding: 14px 12px; }
      .btn-main { font-size: 0.9rem; padding: 10px; }
    }

    /* Preview panel */
    .prev-col {
      flex: 0 0 320px; display: flex; flex-direction: column; gap: 10px;
      align-self: stretch;
      background: var(--panel);
      backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
      border: 1px solid rgba(184,146,74,0.2);
      border-radius: 20px; padding: 16px 14px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.35), inset 0 1px 0 rgba(230,215,188,0.06);
    }
    .prev-col-header {
      display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
    }
    .prev-col-title {
      font-size: 0.56rem; letter-spacing: 0.24em; text-transform: uppercase; color: var(--gold); opacity: 0.7;
      display: flex; align-items: center; gap: 8px; flex: 1;
    }
    .prev-col-title::after { content: ''; flex: 1; height: 1px; background: rgba(184,146,74,0.18); }
    #prev-expand-btn {
      background: none; border: 1px solid rgba(184,146,74,0.18); color: var(--muted);
      border-radius: 7px; width: 28px; height: 28px; cursor: pointer; font-size: 0.85rem;
      display: none; align-items: center; justify-content: center; flex-shrink: 0; margin-left: 8px;
      transition: border-color 0.2s, color 0.2s;
    }
    #prev-expand-btn.visible { display: flex; }
    #prev-expand-btn:hover { border-color: var(--gold); color: var(--gold); }
    #prev-panel {
      flex: 1; min-height: 0; border-radius: 12px;
      border: 1px solid rgba(184,146,74,0.14); background: rgba(0,0,0,0.35);
      overflow: auto; display: flex; align-items: flex-start; justify-content: center;
      position: relative; cursor: default;
    }
    #prev-panel.has-img { cursor: zoom-in; }
    #prev-img { width: 100%; height: auto; display: none; border-radius: 6px; pointer-events: none; }
    #prev-placeholder {
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      gap: 10px; padding: 30px; text-align: center; color: var(--muted);
      position: absolute; inset: 0;
    }
    .prev-ph-icon { font-size: 2.8rem; opacity: 0.2; }
    .prev-ph-txt { font-family: 'Cormorant Garamond', serif; font-style: italic; font-size: 0.88rem; opacity: 0.45; line-height: 1.5; max-width: 220px; }
    #prev-spinner-wrap {
      position: absolute; inset: 0; display: none;
      background: rgba(10,8,4,0.65); align-items: center; justify-content: center;
      flex-direction: column; gap: 12px; border-radius: 12px; backdrop-filter: blur(4px);
    }
    #prev-spinner-wrap.active { display: flex; }
    .prev-spin { width: 32px; height: 32px; border: 2px solid rgba(184,146,74,0.15); border-top-color: var(--gold); border-radius: 50%; animation: spin 0.85s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .prev-spin-txt { font-family: 'Cormorant Garamond', serif; font-style: italic; font-size: 0.8rem; color: var(--muted); }
    #prev-err-txt { color: #e08080; font-size: 0.74rem; padding: 16px; text-align: center; display: none; line-height: 1.5; position: absolute; }
    .prev-footer { display: flex; gap: 8px; flex-shrink: 0; }
    #preview-btn {
      flex: 1;
      background: linear-gradient(135deg, rgba(80,120,184,0.18), rgba(60,90,160,0.08));
      border: 1px solid rgba(100,140,220,0.32); color: #88b4ff; border-radius: 10px; padding: 9px;
      font-family: 'Cormorant Garamond', serif; font-size: 0.82rem; font-weight: 600;
      letter-spacing: 0.1em; cursor: pointer; transition: all 0.25s; text-transform: uppercase;
    }
    #preview-btn:hover:not(:disabled) { background: linear-gradient(135deg, rgba(80,120,184,0.32), rgba(60,90,160,0.18)); border-color: #88b4ff; box-shadow: 0 0 14px rgba(100,140,220,0.2); }
    #preview-btn:disabled { opacity: 0.35; cursor: not-allowed; }

    /* Fullscreen lightbox */
    #lightbox {
      position: fixed; inset: 0; z-index: 600; display: none;
      background: rgba(8,6,3,0.96); backdrop-filter: blur(16px);
      align-items: center; justify-content: center; padding: 20px;
      cursor: zoom-out;
    }
    #lightbox.open { display: flex; }
    #lightbox-img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 8px; box-shadow: 0 0 80px rgba(184,146,74,0.12); pointer-events: none; }
    #lightbox-close {
      position: absolute; top: 16px; right: 16px;
      background: rgba(28,22,14,0.9); border: 1px solid rgba(184,146,74,0.28); color: var(--gold-lt);
      border-radius: 10px; width: 40px; height: 40px; cursor: pointer; font-size: 1.1rem;
      display: flex; align-items: center; justify-content: center; transition: all 0.2s; z-index: 1;
    }
    #lightbox-close:hover { border-color: var(--gold); box-shadow: 0 0 16px rgba(184,146,74,0.2); }
    #lightbox-hint {
      position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%);
      font-size: 0.62rem; letter-spacing: 0.18em; text-transform: uppercase; color: var(--muted);
      opacity: 0.45; pointer-events: none;
    }
  </style>
</head>
<body>
  <canvas id="bg-canvas"></canvas>
  <div id="tip"></div>


  <div id="app">
    <header>
      <div class="eyebrow">&#x2736; &nbsp; Celestial Design Studio &nbsp; &#x2736;</div>
      <h1>PDF Beautifier</h1>
      <p class="subtitle">Choose your sign &nbsp;&middot;&nbsp; Upload your scroll &nbsp;&middot;&nbsp; Receive art</p>
    </header>

    <main>
      <div class="wheel-col">
        <svg id="zodiac-svg" viewBox="0 0 600 600" xmlns="http://www.w3.org/2000/svg"></svg>
        <div id="drop-zone">
          <input type="file" id="file-input" accept=".pdf">
          <div class="dz-moon">&#x263D;</div>
          <div class="dz-lbl">Drop PDF here</div>
          <div class="dz-sub">or click to browse</div>
          <div id="fname-badge">
            <div class="fname-check">&#x2714;</div>
            <div class="fname-label">PDF Loaded</div>
            <div class="fname-name" id="fname"></div>
            <div class="fname-reupload" onclick="fi.click()">&#x21BA; reupload</div>
          </div>
        </div>
      </div>

      <div class="ctrl-col">
        <div class="sign-card" id="sign-card">
          <div class="sign-glyph" id="sign-glyph">&#x2736;</div>
          <div>
            <div class="sign-name" id="sign-name">Select a Zodiac Sign</div>
            <div class="sign-meta" id="sign-meta">Click any segment of the wheel</div>
          </div>
        </div>

        <div class="colors-wrap">
          <div class="sec-ttl">Theme Colors</div>
          <div class="color-row">
            <span class="color-lbl">Background</span>
            <div class="color-grp">
              <input type="color" id="bg-color" value="#0b0d17">
              <input type="text" id="bg-hex" class="hex-in" value="#0B0D17" maxlength="7">
            </div>
          </div>
          <div class="color-row">
            <span class="color-lbl">Text</span>
            <div class="color-grp">
              <input type="color" id="text-color" value="#d1d5db">
              <input type="text" id="text-hex" class="hex-in" value="#D1D5DB" maxlength="7">
            </div>
          </div>
          <div class="color-row">
            <span class="color-lbl" title="Color for bold/highlighted text (dates, headings, planet names)">Bold Text</span>
            <div class="color-grp">
              <input type="color" id="gold-color" value="#ffd85a">
              <input type="text" id="gold-hex" class="hex-in" value="#FFD85A" maxlength="7">
            </div>
          </div>
          <div class="op-wrap">
            <div class="op-lbl"><span>Opacity</span><span id="op-val">100%</span></div>
            <input type="range" id="bg-op" min="0" max="100" value="100">
          </div>
          <div id="preview" style="background:#0b0d17;color:#d1d5db;">Aa &mdash; The celestial canvas awaits</div>
        </div>

        <div style="display:none">
          <input type="color" id="violet-color" value="#ffb266">
          <input type="color" id="teal-color" value="#ffe699">
          <input type="color" id="coral-color" value="#ffc794">
        </div>

        <div class="sec-ttl" style="margin-top:2px;">Output Mode</div>
        <div class="mode-toggle-wrap">
          <div class="mode-opt active" id="mode-quality" onclick="setMode('quality')">
            <div class="mode-icon">&#x2605;</div>
            <div class="mode-info">
              <div class="mode-name">High Quality</div>
              <div class="mode-desc">Pixel-perfect &bull; all 3 colors &bull; ~35MB</div>
            </div>
          </div>
          <div class="mode-opt" id="mode-small" onclick="setMode('small')">
            <div class="mode-icon">&#x26A1;</div>
            <div class="mode-info">
              <div class="mode-name">Small Size</div>
              <div class="mode-desc">All colors &bull; works everywhere &bull; ~8MB</div>
            </div>
          </div>
        </div>

        <button class="btn-main" id="beautify-btn" onclick="uploadFile()" disabled>&#x2736; &nbsp; Beautify PDF &nbsp; &#x2736;</button>
        <div id="status"></div>
        <button id="dl-btn" onclick="downloadFile()">&#x2B07; &nbsp; Download Beautified PDF</button>
      </div>

      <!-- Live preview panel -->
      <div class="prev-col">
        <div class="prev-col-header">
          <div class="prev-col-title">&#x2606; Live Preview</div>
          <button id="prev-expand-btn" title="Full screen" onclick="openLightbox()">&#x26F6;</button>
        </div>
        <div id="prev-panel" onclick="openLightbox()">
          <div id="prev-placeholder">
            <div class="prev-ph-icon">&#x1F4C4;</div>
            <div class="prev-ph-txt">Upload a PDF &amp; select a sign to preview page 1</div>
          </div>
          <img id="prev-img" alt="Preview of page 1">
          <div id="prev-spinner-wrap">
            <div class="prev-spin"></div>
            <div class="prev-spin-txt">Casting the celestial spell&#x2026;</div>
          </div>
          <div id="prev-err-txt"></div>
        </div>
        <div class="prev-footer">
          <button id="preview-btn" onclick="requestPreview()" disabled>&#x21BA; &nbsp; Refresh Preview</button>
        </div>
      </div>
    </main>

    <!-- Fullscreen lightbox -->
    <div id="lightbox" onclick="closeLightbox()">
      <button id="lightbox-close" onclick="closeLightbox()">&#x2715;</button>
      <img id="lightbox-img" alt="Full size preview">
      <div id="lightbox-hint">Click anywhere to close &nbsp;&middot;&nbsp; Esc</div>
    </div>
  </div>

  <script>
  // ══════════════ ZODIAC DATA ══════════════
  const SIGNS = [
    {name:'Aries',       sym:'\u2648', bg:'#1a0500', text:'#ff9966', accent:'#ffd85a', el:'Fire',  dates:'Mar 21 \u2013 Apr 19'},
    {name:'Taurus',      sym:'\u2649', bg:'#0a1a0a', text:'#88d488', accent:'#c8f0a0', el:'Earth', dates:'Apr 20 \u2013 May 20'},
    {name:'Gemini',      sym:'\u264A', bg:'#04101f', text:'#88ccff', accent:'#ffe08a', el:'Air',   dates:'May 21 \u2013 Jun 20'},
    {name:'Cancer',      sym:'\u264B', bg:'#0d1b2a', text:'#c8d8f0', accent:'#d0c0ff', el:'Water', dates:'Jun 21 \u2013 Jul 22'},
    {name:'Leo',         sym:'\u264C', bg:'#1c0f00', text:'#ffd700', accent:'#ff9944', el:'Fire',  dates:'Jul 23 \u2013 Aug 22'},
    {name:'Virgo',       sym:'\u264D', bg:'#f0ece0', text:'#2c1a0e', accent:'#b05010', el:'Earth', dates:'Aug 23 \u2013 Sep 22'},
    {name:'Libra',       sym:'\u264E', bg:'#180a14', text:'#ffaac8', accent:'#ffdd88', el:'Air',   dates:'Sep 23 \u2013 Oct 22'},
    {name:'Scorpio',     sym:'\u264F', bg:'#0b0018', text:'#b080ff', accent:'#ff6688', el:'Water', dates:'Oct 23 \u2013 Nov 21'},
    {name:'Sagittarius', sym:'\u2650', bg:'#100a00', text:'#ffcc66', accent:'#ff8844', el:'Fire',  dates:'Nov 22 \u2013 Dec 21'},
    {name:'Capricorn',   sym:'\u2651', bg:'#080808', text:'#b0b0c0', accent:'#88aacc', el:'Earth', dates:'Dec 22 \u2013 Jan 19'},
    {name:'Aquarius',    sym:'\u2652', bg:'#030c18', text:'#00d4ff', accent:'#88eeff', el:'Air',   dates:'Jan 20 \u2013 Feb 18'},
    {name:'Pisces',      sym:'\u2653', bg:'#04081a', text:'#66b8dd', accent:'#aa88ff', el:'Water', dates:'Feb 19 \u2013 Mar 20'},
  ];
  const EC = {Fire:'#e8622a', Earth:'#5a9e48', Air:'#3aa8d8', Water:'#4060cc'};

  // ══════════════ BUILD ZODIAC SVG ══════════════
  const NS = 'http://www.w3.org/2000/svg';
  const CX=300, CY=300, RO=275, RI=130, RT=213, RS=167, GAP=1.5;

  function el(tag,attrs){ const e=document.createElementNS(NS,tag); for(const[k,v] of Object.entries(attrs)) e.setAttribute(k,v); return e; }
  function polar(r,deg){ const a=(deg-90)*Math.PI/180; return [CX+r*Math.cos(a), CY+r*Math.sin(a)]; }
  function arc(r1,r2,a1,a2){
    const[x1,y1]=polar(r1,a1),[x2,y2]=polar(r2,a1),[x3,y3]=polar(r2,a2),[x4,y4]=polar(r1,a2);
    const lg=(a2-a1)>180?1:0;
    return 'M '+x1+' '+y1+' L '+x2+' '+y2+' A '+r2+' '+r2+' 0 '+lg+' 1 '+x3+' '+y3+' L '+x4+' '+y4+' A '+r1+' '+r1+' 0 '+lg+' 0 '+x1+' '+y1+' Z';
  }

  const SVG = document.getElementById('zodiac-svg');
  const defs = el('defs',{});

  SIGNS.forEach((_,i)=>{
    const f=el('filter',{id:'gf'+i,x:'-35%',y:'-35%',width:'170%',height:'170%'});
    const fb=el('feGaussianBlur',{in:'SourceGraphic',stdDeviation:'7',result:'b'});
    const fm=el('feMerge',{}); fm.appendChild(el('feMergeNode',{in:'b'})); fm.appendChild(el('feMergeNode',{in:'SourceGraphic'}));
    f.appendChild(fb); f.appendChild(fm); defs.appendChild(f);
  });
  SVG.appendChild(defs);

  // Decorative outer rings
  SVG.appendChild(el('circle',{cx:CX,cy:CY,r:RO+9,fill:'none',stroke:'rgba(201,168,76,0.12)',  'stroke-width':'0.7'}));
  SVG.appendChild(el('circle',{cx:CX,cy:CY,r:RO+16,fill:'none',stroke:'rgba(201,168,76,0.05)', 'stroke-width':'0.5','stroke-dasharray':'3 9'}));

  // Degree tick marks
  for(let d=0;d<360;d+=5){
    const isSign=d%30===0, isMid=d%15===0&&!isSign;
    const outerR=RO+(isSign?8:isMid?4:2);
    const[x1,y1]=polar(RO,d),[x2,y2]=polar(outerR,d);
    SVG.appendChild(el('line',{x1,y1,x2,y2,stroke:'rgba(201,168,76,'+(isSign?'0.4':isMid?'0.18':'0.08')+')', 'stroke-width':isSign?'1.2':'0.5'}));
  }

  // Pulse ring (shown on sign select)
  const pRing = el('circle',{cx:CX,cy:CY,r:RO+2,fill:'none',stroke:'rgba(201,168,76,0.4)','stroke-width':'1.5',opacity:'0',id:'pring'});
  SVG.appendChild(pRing);

  const segs=[];
  SIGNS.forEach((s,i)=>{
    const a1=i*30+GAP, a2=(i+1)*30-GAP, amid=i*30+15;
    const ec=EC[s.el];
    const g=el('g',{class:'seg','data-i':i});

    // Base segment
    const sf=el('path',{d:arc(RI+6,RO,a1,a2),fill:'rgba(255,255,255,0.025)',stroke:'rgba(201,168,76,0.1)','stroke-width':'0.5',class:'sf'});
    g.appendChild(sf);

    // Glow overlay
    const gf=el('path',{d:arc(RI+6,RO,a1,a2),fill:ec+'1c',stroke:ec,'stroke-width':'1.5',opacity:'0',class:'gf',style:'filter:url(#gf'+i+')'});
    g.appendChild(gf);

    // Element accent bar (inner rim strip)
    g.appendChild(el('path',{d:arc(RI+6,RI+13,a1,a2),fill:ec+'60',class:'ab'}));

    // Zodiac symbol
    const[sx,sy]=polar(RS,amid);
    const sym=el('text',{x:sx,y:sy,'text-anchor':'middle','dominant-baseline':'central','font-size':'17.5',fill:'rgba(201,168,76,0.72)',class:'sym',style:'pointer-events:none;font-family:serif;'});
    sym.textContent=s.sym; g.appendChild(sym);

    // Sign name
    const[nx,ny]=polar(RT,amid);
    const nm=el('text',{x:nx,y:ny,'text-anchor':'middle','dominant-baseline':'central','font-size':'7',fill:'rgba(200,190,220,0.38)',class:'nm',style:'pointer-events:none;font-family:"Space Grotesk";letter-spacing:.12em;text-transform:uppercase;'});
    nm.textContent=s.name.toUpperCase(); g.appendChild(nm);

    // Outer element dot
    const[dx,dy]=polar(RO-9,amid);
    g.appendChild(el('circle',{cx:dx,cy:dy,r:'2.8',fill:ec+'90',class:'dot'}));

    g.style.webkitTapHighlightColor = 'transparent';
    g.style.outline = 'none';
    g.addEventListener('touchstart', e=>{ e.preventDefault(); selectSign(i); }, {passive:false});
    g.addEventListener('click',()=>selectSign(i));
    const tip=document.getElementById('tip');
    g.addEventListener('mousemove',e2=>{
      tip.innerHTML=s.sym+' '+s.name+' &nbsp;&middot;&nbsp; '+s.el+' &nbsp;&middot;&nbsp; '+s.dates;
      tip.style.display='block'; tip.style.left=(e2.clientX+14)+'px'; tip.style.top=(e2.clientY-8)+'px';
    });
    g.addEventListener('mouseleave',()=>tip.style.display='none');

    SVG.appendChild(g); segs.push(g);
  });

  // Radial dividers
  for(let i=0;i<12;i++){
    const[x1,y1]=polar(RI+6,i*30),[x2,y2]=polar(RO,i*30);
    SVG.appendChild(el('line',{x1,y1,x2,y2,stroke:'rgba(201,168,76,0.1)','stroke-width':'0.6'}));
  }

  // Inner rings
  SVG.appendChild(el('circle',{cx:CX,cy:CY,r:RI+6,fill:'none',stroke:'rgba(201,168,76,0.22)','stroke-width':'1'}));
  SVG.appendChild(el('circle',{cx:CX,cy:CY,r:RI,  fill:'none',stroke:'rgba(201,168,76,0.08)','stroke-width':'0.5'}));

  // Center labels
  [['CELESTIAL',RI-18],['STUDIO',RI-9]].forEach(([t,off])=>{
    const tx=el('text',{x:CX,y:CY+off,'text-anchor':'middle','font-size':'6.5',fill:'rgba(201,168,76,0.25)',style:'font-family:"Space Grotesk";letter-spacing:.28em;'});
    tx.textContent=t; SVG.appendChild(tx);
  });

  // ══════════════ DROP ZONE SIZING ══════════════
  function sizeDZ(){
    const r=document.getElementById('zodiac-svg').getBoundingClientRect();
    const scale=r.width/600, diam=Math.floor(RI*scale*2*0.9);
    const dz=document.getElementById('drop-zone');
    dz.style.width=diam+'px'; dz.style.height=diam+'px';
    const b=diam/9;
    dz.querySelector('.dz-moon').style.fontSize=(b*1.9)+'px';
    dz.querySelector('.dz-lbl').style.fontSize=(b*0.95)+'px';
    dz.querySelector('.dz-sub').style.fontSize=(b*0.68)+'px';
    document.getElementById('fname').style.fontSize=(b*0.62)+'px';
    const chk=document.querySelector('.fname-check'); if(chk) chk.style.fontSize=(b*1.3)+'px';
    const lbl=document.querySelector('.fname-label'); if(lbl) lbl.style.fontSize=(b*0.55)+'px';
  }
  sizeDZ();
  // Re-run after fonts/layout settle on mobile
  window.addEventListener('load', sizeDZ);
  setTimeout(sizeDZ, 300);

  // ══════════════ SELECT SIGN ══════════════
  let activeIdx=-1;
  function selectSign(i){
    segs.forEach((g,j)=>{
      const on=j===i;
      g.querySelector('.gf').style.opacity=on?'1':'0';
      g.querySelector('.sf').style.fill=on?(SIGNS[j].bg+'66'):'rgba(255,255,255,0.025)';
      g.querySelector('.sym').style.fill=on?SIGNS[j].text:'rgba(201,168,76,0.72)';
      g.querySelector('.nm').style.fill=on?'rgba(200,190,220,0.72)':'rgba(200,190,220,0.38)';
    });
    activeIdx=i;
    const s=SIGNS[i], ec=EC[s.el];
    const gh=document.getElementById('sign-glyph');
    gh.textContent=s.sym; gh.style.color=s.text; gh.style.filter='drop-shadow(0 0 12px '+s.text+'88)';
    document.getElementById('sign-name').textContent=s.name;
    document.getElementById('sign-name').style.color=s.text;
    document.getElementById('sign-meta').textContent='\u2736 '+s.el+' \u00b7 '+s.dates;
    document.getElementById('sign-meta').style.color=ec;
    const sc=document.getElementById('sign-card');
    sc.style.borderColor=ec+'50'; sc.style.background='linear-gradient(135deg,'+ec+'0e,rgba(255,255,255,0.01))';

    // Pulse ring
    const pr=document.getElementById('pring');
    pr.setAttribute('stroke',ec); pr.style.opacity='0';
    pr.classList.remove('pulse-ring'); void pr.offsetWidth; pr.classList.add('pulse-ring');
    setTimeout(()=>pr.style.opacity='0', 2200);

    document.getElementById('bg-color').value=s.bg.length===7?s.bg:'#'+s.bg.slice(1).padStart(6,'0');
    document.getElementById('text-color').value=s.text;
    document.getElementById('bg-hex').value=s.bg.toUpperCase();
    document.getElementById('text-hex').value=s.text.toUpperCase();
    document.getElementById('gold-color').value=s.accent;
    document.getElementById('gold-hex').value=s.accent.toUpperCase();
    document.getElementById('bg-op').value=100;
    syncA(); updatePrev(); schedulePreview();
  }

  // ══════════════ COLORS ══════════════
  const bgPk=document.getElementById('bg-color'), txPk=document.getElementById('text-color');

  function h2r(h){return[parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];}
  function applyOp(hex,op){const[r,g,b]=h2r(hex),o=op/100;return '#'+[r,g,b].map(v=>Math.round(v*o+255*(1-o)).toString(16).padStart(2,'0')).join('');}
  function vHex(v){return /^#[0-9a-fA-F]{6}$/.test(v);}

  function updatePrev(){
    const eff=applyOp(bgPk.value,parseInt(document.getElementById('bg-op').value));
    document.getElementById('preview').style.background=eff;
    document.getElementById('preview').style.color=txPk.value;
    document.getElementById('op-val').textContent=document.getElementById('bg-op').value+'%';
  }
  const acPk=document.getElementById('gold-color');

  function syncA(){
    const v=txPk.value;
    ['violet-color','teal-color','coral-color'].forEach(id=>document.getElementById(id).value=v);
    updatePrev();
  }

  bgPk.addEventListener('input',()=>{document.getElementById('bg-hex').value=bgPk.value.toUpperCase();updatePrev();schedulePreview();});
  document.getElementById('bg-hex').addEventListener('input',e=>{
    const v=e.target.value.startsWith('#')?e.target.value:'#'+e.target.value;
    if(vHex(v)){bgPk.value=v;updatePrev();schedulePreview();}
  });
  txPk.addEventListener('input',()=>{document.getElementById('text-hex').value=txPk.value.toUpperCase();syncA();schedulePreview();});
  document.getElementById('text-hex').addEventListener('input',e=>{
    const v=e.target.value.startsWith('#')?e.target.value:'#'+e.target.value;
    if(vHex(v)){txPk.value=v;syncA();}
  });
  acPk.addEventListener('input',()=>{document.getElementById('gold-hex').value=acPk.value.toUpperCase();schedulePreview();});
  document.getElementById('gold-hex').addEventListener('input',e=>{
    const v=e.target.value.startsWith('#')?e.target.value:'#'+e.target.value;
    if(vHex(v)){acPk.value=v;schedulePreview();}
  });
  document.getElementById('bg-op').addEventListener('input',updatePrev);

  // ══════════════ FILE UPLOAD ══════════════
  let curJob=null, poll=null, currentMode='quality';
  function setMode(m){
    currentMode=m;
    document.getElementById('mode-quality').classList.toggle('active', m==='quality');
    document.getElementById('mode-small').classList.toggle('active', m==='small');
    schedulePreview();
  }
  const dz=document.getElementById('drop-zone'), fi=document.getElementById('file-input');
  const bBtn=document.getElementById('beautify-btn'), st=document.getElementById('status'), dlBtn=document.getElementById('dl-btn');

  dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('dragover');});
  dz.addEventListener('dragleave',()=>dz.classList.remove('dragover'));
  dz.addEventListener('drop',e=>{
    e.preventDefault(); dz.classList.remove('dragover');
    const f=e.dataTransfer.files[0];
    if(f&&f.name.endsWith('.pdf')){fi.files=e.dataTransfer.files; handleFile();}
  });
  dz.addEventListener('click',()=>fi.click());
  fi.addEventListener('change',handleFile);

  function handleFile(){
    const f=fi.files[0]; if(!f) return;
    document.getElementById('fname').textContent=f.name;
    const badge=document.getElementById('fname-badge');
    badge.style.display='flex';
    dz.querySelector('.dz-moon').style.display='none';
    dz.querySelector('.dz-lbl').style.display='none';
    dz.querySelector('.dz-sub').style.display='none';
    bBtn.disabled=false;
    document.getElementById('preview-btn').disabled=false;
    requestPreview();
  }

  // ══════════════ LIVE PREVIEW ══════════════
  let prevDebounce=null;
  function schedulePreview(){
    // Re-trigger preview when colors change (if file already loaded)
    if(!fi.files[0]) return;
    clearTimeout(prevDebounce);
    prevDebounce=setTimeout(requestPreview, 600);
  }

  async function requestPreview(){
    const f=fi.files[0]; if(!f) return;
    const pb=document.getElementById('preview-btn');
    const sw=document.getElementById('prev-spinner-wrap');
    const img=document.getElementById('prev-img');
    const errTxt=document.getElementById('prev-err-txt');
    const ph=document.getElementById('prev-placeholder');
    const panel=document.getElementById('prev-panel');
    const expBtn=document.getElementById('prev-expand-btn');

    pb.disabled=true;
    sw.classList.add('active');
    errTxt.style.display='none';
    ph.style.display='none';

    const fd=new FormData();
    fd.append('file',f);
    fd.append('bg_color',applyOp(bgPk.value,parseInt(document.getElementById('bg-op').value)));
    fd.append('text_color',txPk.value);
    ['gold','violet','teal','coral'].forEach(n=>fd.append(n+'_color',document.getElementById(n+'-color').value));
    fd.append('mode', currentMode);
    try{
      const r=await fetch('/preview',{method:'POST',body:fd});
      if(!r.ok){
        const d=await r.json().catch(()=>({}));
        throw new Error(d.detail||'Preview failed');
      }
      const blob=await r.blob();
      const oldUrl=img.src;
      const url=URL.createObjectURL(blob);
      img.onload=()=>{
        sw.classList.remove('active');
        img.style.display='block';
        panel.classList.add('has-img');
        expBtn.classList.add('visible');
        if(oldUrl.startsWith('blob:')) URL.revokeObjectURL(oldUrl);
        // Also update lightbox src
        document.getElementById('lightbox-img').src=url;
      };
      img.src=url;
    }catch(err){
      sw.classList.remove('active');
      img.style.display='none';
      panel.classList.remove('has-img');
      expBtn.classList.remove('visible');
      errTxt.textContent='\u2715 '+err.message;
      errTxt.style.display='block';
    }finally{
      pb.disabled=false;
    }
  }

  // ══════════════ LIGHTBOX ══════════════
  function openLightbox(){
    const src=document.getElementById('prev-img').src;
    if(!src||!src.startsWith('blob:')) return;
    document.getElementById('lightbox').classList.add('open');
    document.body.style.overflow='hidden';
  }
  function closeLightbox(){
    document.getElementById('lightbox').classList.remove('open');
    document.body.style.overflow='';
  }
  document.addEventListener('keydown',e=>{if(e.key==='Escape') closeLightbox();});

  async function uploadFile(){
    const f=fi.files[0]; if(!f) return;
    if(poll){clearInterval(poll);poll=null;}
    curJob=null; bBtn.disabled=true; dlBtn.style.display='none';
    showSt('processing','\u23F3 Weaving your scroll through the cosmos\u2026');
    const fd=new FormData();
    fd.append('file',f);
    fd.append('bg_color',applyOp(bgPk.value,parseInt(document.getElementById('bg-op').value)));
    fd.append('text_color',txPk.value);
    ['gold','violet','teal','coral'].forEach(n=>fd.append(n+'_color',document.getElementById(n+'-color').value));
    fd.append('mode', currentMode);
    try{
      const r=await fetch('/upload',{method:'POST',body:fd});
      const d=await r.json();
      if(!r.ok) throw new Error(d.detail||'Upload failed');
      curJob=d.job_id; pollSt();
    }catch(err){showSt('error','\u2715 '+err.message);bBtn.disabled=false;}
  }

  function pollSt(){
    poll=setInterval(async()=>{
      try{
        const r=await fetch('/status/'+curJob);
        const d=await r.json();
        if(d.status==='done'){clearInterval(poll);showSt('done','\u2736 Your celestial PDF is ready!');dlBtn.style.display='block';bBtn.disabled=false;boom();}
        else if(d.status==='error'){clearInterval(poll);showSt('error','\u2715 '+(d.error||'Processing failed'));bBtn.disabled=false;}
      }catch(e){clearInterval(poll);showSt('error','\u2715 Connection lost');bBtn.disabled=false;}
    },2000);
  }
  function downloadFile(){if(curJob) window.location.href='/download/'+curJob;}
  function showSt(type,msg){st.className=type;st.style.display='block';st.textContent=msg;}
  function boom(){
    const c=['#c9a84c','#e8c97a','#fff','#b080ff','#66b8dd'];
    confetti({particleCount:80,spread:70,origin:{y:0.6},colors:c});
    setTimeout(()=>confetti({particleCount:50,angle:60,spread:55,origin:{x:0},colors:c}),300);
    setTimeout(()=>confetti({particleCount:50,angle:120,spread:55,origin:{x:1},colors:c}),500);
  }

  // ══════════════ THREE.JS CONSTELLATION BG ══════════════
  (function(){
    const canvas=document.getElementById('bg-canvas');
    const renderer=new THREE.WebGLRenderer({canvas,antialias:true});
    renderer.setPixelRatio(Math.min(devicePixelRatio,2));
    renderer.setSize(innerWidth,innerHeight);
    renderer.setClearColor(0x1c1810);

    const scene=new THREE.Scene();
    const camera=new THREE.PerspectiveCamera(65,innerWidth/innerHeight,0.1,3000);
    camera.position.set(0,0,400);

    function mkStarTex(){
      const c=document.createElement('canvas'); c.width=c.height=32;
      const ctx=c.getContext('2d');
      const g=ctx.createRadialGradient(16,16,0,16,16,16);
      g.addColorStop(0,'rgba(255,248,220,1)'); g.addColorStop(0.2,'rgba(210,180,255,0.55)'); g.addColorStop(1,'rgba(0,0,0,0)');
      ctx.fillStyle=g; ctx.fillRect(0,0,32,32);
      return new THREE.CanvasTexture(c);
    }
    const tex=mkStarTex();

    const N=1500, pts=[], pos=new Float32Array(N*3), col=new Float32Array(N*3);
    for(let i=0;i<N;i++){
      const x=(Math.random()-.5)*980, y=(Math.random()-.5)*740, z=(Math.random()-.5)*320-100;
      pts.push([x,y,z]); pos[i*3]=x; pos[i*3+1]=y; pos[i*3+2]=z;
      const t=Math.random();
      if(t<.48){col[i*3]=.93;col[i*3+1]=.9;col[i*3+2]=1;}
      else if(t<.7){col[i*3]=1;col[i*3+1]=.93;col[i*3+2]=.7;}
      else if(t<.86){col[i*3]=.78;col[i*3+1]=.56;col[i*3+2]=1;}
      else{col[i*3]=.55;col[i*3+1]=.78;col[i*3+2]=1;}
    }
    const sg=new THREE.BufferGeometry();
    sg.setAttribute('position',new THREE.BufferAttribute(pos,3));
    sg.setAttribute('color',new THREE.BufferAttribute(col,3));
    const stars=new THREE.Points(sg,new THREE.PointsMaterial({size:1.4,map:tex,vertexColors:true,transparent:true,alphaTest:.01,sizeAttenuation:true,depthWrite:false}));
    scene.add(stars);

    // Constellation lines
    const lp=[]; let cnt=0;
    for(let i=0;i<N&&cnt<240;i++){
      for(let j=i+1;j<N&&cnt<240;j++){
        const dx=pts[i][0]-pts[j][0],dy=pts[i][1]-pts[j][1],dz=pts[i][2]-pts[j][2];
        if(Math.sqrt(dx*dx+dy*dy+dz*dz)<70&&Math.random()<.3){lp.push(...pts[i],...pts[j]);cnt++;}
      }
    }
    const lg=new THREE.BufferGeometry();
    lg.setAttribute('position',new THREE.BufferAttribute(new Float32Array(lp),3));
    const lines=new THREE.LineSegments(lg,new THREE.LineBasicMaterial({color:0xb8924a,transparent:true,opacity:.12}));
    scene.add(lines);

    // Nebulae
    function mkNeb(hex,a){
      const c=document.createElement('canvas'); c.width=c.height=128;
      const ctx=c.getContext('2d');
      const r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);
      const gr=ctx.createRadialGradient(64,64,0,64,64,64);
      gr.addColorStop(0,'rgba('+r+','+g+','+b+','+a+')'); gr.addColorStop(.45,'rgba('+r+','+g+','+b+','+(a*.3)+')'); gr.addColorStop(1,'rgba(0,0,0,0)');
      ctx.fillStyle=gr; ctx.fillRect(0,0,128,128);
      return new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c),transparent:true,blending:THREE.AdditiveBlending,depthWrite:false});
    }
    [{hex:'#6b3a10',a:.28,x:110,y:60,z:-90,s:460},{hex:'#8c3a20',a:.2,x:-140,y:-50,z:70,s:380},{hex:'#3a2a60',a:.22,x:50,y:-90,z:110,s:340},{hex:'#5a3818',a:.18,x:-60,y:80,z:-70,s:310}].forEach(d=>{
      const sp=new THREE.Sprite(mkNeb(d.hex,d.a)); sp.position.set(d.x,d.y,d.z); sp.scale.setScalar(d.s); scene.add(sp);
    });

    // Shooting stars
    const ss=[];
    function spawn(){
      const m=new THREE.LineBasicMaterial({color:0xfff8dc,transparent:true,opacity:.8});
      const ox=(Math.random()-.5)*720,oy=90+Math.random()*200,oz=-90-Math.random()*160;
      const len=50+Math.random()*90,ang=Math.PI/5+Math.random()*Math.PI/8;
      const geo=new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(ox,oy,oz),new THREE.Vector3(ox+Math.cos(ang)*len,oy-Math.sin(ang)*len,oz)]);
      const l=new THREE.Line(geo,m); l.userData={vx:9+Math.random()*7,vy:-(4+Math.random()*3),life:1};
      scene.add(l); ss.push(l);
    }

    let mx=0,my=0;
    document.addEventListener('mousemove',e=>{mx=(e.clientX/innerWidth-.5)*2;my=(e.clientY/innerHeight-.5)*2;});

    let t=0;
    (function anim(){
      requestAnimationFrame(anim); t+=.0004;
      stars.rotation.y=t*.065; stars.rotation.x=t*.025;
      lines.rotation.y=t*.065; lines.rotation.x=t*.025;
      camera.position.x+=(mx*18-camera.position.x)*.018;
      camera.position.y+=(-my*12-camera.position.y)*.018;
      camera.lookAt(0,0,0);
      if(Math.random()<.003) spawn();
      for(let i=ss.length-1;i>=0;i--){
        const s=ss[i]; s.userData.life-=.016; s.material.opacity=s.userData.life*.8;
        s.position.x+=s.userData.vx; s.position.y+=s.userData.vy;
        if(s.userData.life<=0){scene.remove(s);ss.splice(i,1);}
      }
      renderer.render(scene,camera);
    })();

    window.addEventListener('resize',()=>{
      camera.aspect=innerWidth/innerHeight; camera.updateProjectionMatrix();
      renderer.setSize(innerWidth,innerHeight); sizeDZ();
    });
  })();
  </script>
</body>
</html>"""


@app.post("/preview")
async def preview_pdf(
    file: UploadFile = File(...),
    bg_color: str = Form("#0b0d17"),
    text_color: str = Form("#d1d5db"),
    gold_color: str = Form("#ffd85a"),
    violet_color: str = Form("#ffb266"),
    teal_color: str = Form("#ffe699"),
    coral_color: str = Form("#ffc794"),
    mode: str = Form("quality"),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    palette = {
        "bg":     hex_to_rgb(bg_color),
        "text":   hex_to_rgb(text_color),
        "gold":   hex_to_rgb(gold_color),
        "violet": hex_to_rgb(violet_color),
        "teal":   hex_to_rgb(teal_color),
        "coral":  hex_to_rgb(coral_color),
    }

    render_fn = _render_preview_page if mode != "small" else _render_preview_page_small

    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, "input.pdf")

        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            loop = asyncio.get_running_loop()
            png_bytes = await loop.run_in_executor(
                None, render_fn, input_path, palette
            )
        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Preview failed: {e}")

    return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png")


def _render_preview_page(input_path: str, palette: dict) -> bytes:
    """
    Preview strategy: render the ORIGINAL PDF page with PyMuPDF (same engine as
    Chrome — correct fonts, transparency, borders), then apply the palette color
    transformation on the raw pixels using _recolor_array.
    Fast (~1-2s), no pikepdf stream manipulation, visually identical to final output.
    """
    import fitz
    import numpy as np
    from PIL import Image
    import io as _io

    # 1. Render original page at 180 DPI — PyMuPDF reads it correctly like Chrome
    doc = fitz.open(input_path)
    page = doc[0]
    mat = fitz.Matrix(180 / 72, 180 / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    doc.close()

    # 2. Convert pixmap → float32 numpy array [0..1]
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    arr = arr.astype(np.float32) / 255.0

    # 3. Apply the exact same palette color mapping used on embedded images
    with _recolor_lock:
        _set_palette(palette)
        recolored = _recolor_array(arr)

    # 4. Encode result as PNG
    out_arr = (np.clip(recolored, 0, 1) * 255).astype(np.uint8)
    img = Image.fromarray(out_arr, "RGB")
    buf = _io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _render_preview_page_small(input_path: str, palette: dict) -> bytes:
    """
    Preview for Small Size mode: recolor page 1 with pikepdf (vector), then
    render the result with PyMuPDF so the user sees what the vector output looks like.
    """
    import fitz
    import numpy as np
    from PIL import Image
    import io as _io

    from .recolor import recolor_pdf

    with tempfile.TemporaryDirectory() as tmp:
        recolored_path = os.path.join(tmp, "recolored.pdf")
        with _recolor_lock:
            recolor_pdf(input_path, recolored_path, palette, max_pages=1)

        doc = fitz.open(recolored_path)
        page = doc[0]
        mat = fitz.Matrix(180 / 72, 180 / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        doc.close()

    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    img = Image.fromarray(arr, "RGB")
    buf = _io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


@app.post("/upload")
async def upload_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    bg_color: str = Form("#0b0d17"),
    text_color: str = Form("#d1d5db"),
    gold_color: str = Form("#ffd85a"),
    violet_color: str = Form("#ffb266"),
    teal_color: str = Form("#ffe699"),
    coral_color: str = Form("#ffc794"),
    mode: str = Form("quality"),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    job_id = str(uuid.uuid4())
    input_path = UPLOAD_DIR / f"{job_id}_input.pdf"
    output_path = OUTPUT_DIR / f"{job_id}_output.pdf"

    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    palette = {
        "bg":     hex_to_rgb(bg_color),
        "text":   hex_to_rgb(text_color),
        "gold":   hex_to_rgb(gold_color),
        "violet": hex_to_rgb(violet_color),
        "teal":   hex_to_rgb(teal_color),
        "coral":  hex_to_rgb(coral_color),
    }

    jobs[job_id] = {
        "status": "processing",
        "input": str(input_path),
        "output": str(output_path),
        "timestamp": time.time()
    }

    background_tasks.add_task(_process_pdf, job_id, str(input_path), str(output_path), file.filename, palette, mode)

    return {"job_id": job_id, "status": "processing"}


async def _process_pdf(job_id: str, input_path: str, output_path: str, original_filename: str, palette: dict, mode: str = "quality"):
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _process_pdf_sync, input_path, output_path, original_filename, palette, mode)
        jobs[job_id]["status"] = "done"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        traceback.print_exc()


def _process_pdf_sync(input_path: str, output_path: str, original_filename: str, palette: dict, mode: str = "quality"):
    if mode == "small":
        from .recolor import recolor_pdf
        print(f"[DEBUG] recoloring via pikepdf (small size)")
        with _recolor_lock:
            recolor_pdf(input_path, output_path, palette)
        print("[DEBUG] done")
        return

    import fitz
    import numpy as np
    from PIL import Image
    import io as _io
    import math

    DPI, QUALITY = 200, 85
    print(f"[DEBUG] recoloring via PyMuPDF pixel render (high quality)")

    src = fitz.open(input_path)
    
    if len(src) > 50:
        DPI = int(200 * math.sqrt(50 / len(src)))
        DPI = max(72, min(200, DPI))
        QUALITY = min(85, max(60, int(85 * (50 / len(src)))))

    out_doc = fitz.open()

    with _recolor_lock:
        _set_palette(palette)
        for page_num in range(len(src)):
            page = src[page_num]
            mat = fitz.Matrix(DPI / 72, DPI / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            arr = arr.astype(np.float32) / 255.0
            recolored = _recolor_array(arr)
            out_arr = (np.clip(recolored, 0, 1) * 255).astype(np.uint8)
            img = Image.fromarray(out_arr, "RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=QUALITY)
            buf.seek(0)
            new_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=buf.read())

    src.close()
    out_doc.save(output_path, deflate=True)
    out_doc.close()
    print("[DEBUG] done")


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    return {"status": job["status"], "error": job.get("error")}


@app.get("/download/{job_id}")
async def download_pdf(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="PDF not ready yet")
    output_path = job["output"]
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(output_path, media_type="application/pdf", filename="beautified.pdf")
