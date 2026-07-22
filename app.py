import gradio as gr
import gradio.routes
from fastapi import WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
import os
import sys
import pty
import subprocess
import struct
import fcntl
import termios
import asyncio
import json
import signal
import shutil
import time
from collections import deque

# ================= 1. PTY 会话管理器 =================

class TerminalSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.master_fd = None
        self.slave_fd = None
        self.process = None
        self.cmd = ["/bin/bash"]
        self.running = False
        self.history = deque(maxlen=5000) 
        self.active_websockets = set()
        self.read_task = None
        self.created_at = time.time() # 记录创建时间，用于排序

    def start(self):
        if self.running and self.process and self.process.poll() is None:
            return 
        self._kill_process()
        self.master_fd, self.slave_fd = pty.openpty()
        self.process = subprocess.Popen(
            self.cmd, stdin=self.slave_fd, stdout=self.slave_fd, stderr=self.slave_fd,
            preexec_fn=os.setsid, close_fds=True
        )
        self.running = True
        os.close(self.slave_fd)
        self.read_task = asyncio.create_task(self._background_reader())
        print(f"Session {self.session_id}: Started")

    def _kill_process(self):
        self.running = False
        if self.read_task: self.read_task.cancel()
        if self.master_fd:
            try: os.close(self.master_fd)
            except: pass
            self.master_fd = None
        if self.process:
            try: os.killpg(os.getpgid(self.process.pid), signal.SIGTERM); self.process.wait(timeout=1)
            except: pass
            self.process = None

    def restart(self):
        self._kill_process()
        self.history.clear()
        self._broadcast("\x1bc\r\n\x1b[33m--- Manual System Reset ---\x1b[0m\r\n")
        self.start()

    def close(self):
        self._kill_process()
        self.history.clear()
        self.active_websockets.clear()

    async def _background_reader(self):
        loop = asyncio.get_event_loop()
        while self.running and self.master_fd:
            try:
                data = await loop.run_in_executor(None, os.read, self.master_fd, 10240)
                if not data: break
                text = data.decode('utf-8', errors='ignore')
                self.history.append(text)
                self._broadcast(text)
            except: break
        self.running = False

    def _broadcast(self, text):
        dead = set()
        for ws in self.active_websockets:
            try: asyncio.create_task(ws.send_text(text))
            except: dead.add(ws)
        for ws in dead: self.active_websockets.discard(ws)

    async def connect(self, ws):
        self.active_websockets.add(ws)
        if self.history: await ws.send_text("".join(self.history))

    def disconnect(self, ws):
        self.active_websockets.discard(ws)

    def write(self, data):
        if self.running and self.master_fd:
            try: os.write(self.master_fd, data.encode())
            except: pass
        else:
            self.start()
            if self.running:
                try: os.write(self.master_fd, data.encode())
                except: pass

    def resize(self, rows, cols):
        if self.running and self.master_fd:
            try: fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except: pass

# === 全局会话注册表 ===
SESSIONS = {}

def get_session(session_id):
    if session_id not in SESSIONS:
        SESSIONS[session_id] = TerminalSession(session_id)
    return SESSIONS[session_id]

def remove_session(session_id):
    if session_id in SESSIONS:
        SESSIONS[session_id].close()
        del SESSIONS[session_id]

# ================= 2. 文件管理逻辑 =================

def get_file_info(path):
    if not os.path.exists(path): return [], "❌ 路径不存在"
    items = []
    try:
        scanned = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
        parent = os.path.dirname(path)
        if parent and parent != path: items.append(["📂", "..", "Parent Dir", "", parent])
        for entry in scanned:
            icon = "📂" if entry.is_dir() else "📄"
            size_str = ""
            if entry.is_file():
                size = entry.stat().st_size
                for unit in ['B', 'KB', 'MB', 'GB']:
                    if size < 1024: size_str = f"{size:.1f} {unit}"; break
                    size /= 1024
            mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(entry.stat().st_mtime))
            items.append([icon, entry.name, size_str, mtime, entry.path])
    except Exception as e: return [], f"❌ 错误: {str(e)}"
    return items, None

def handle_open(evt: gr.SelectData, current_path, file_list_state):
    data = file_list_state
    if hasattr(file_list_state, "values"): data = file_list_state.values.tolist()
    if not data or len(data) <= evt.index[0]: return current_path, None, None, "❌ 无效"
    selected = data[evt.index[0]]
    if selected[1] == "..":
        p = os.path.dirname(current_path); l, m = get_file_info(p)
        return p, l, None, f"📂 {p}"
    if selected[0] == "📂":
        l, m = get_file_info(selected[4])
        return (current_path, None, None, m) if m else (selected[4], l, None, f"📂 {selected[4]}")
    return current_path, None, selected[4], f"⬇️ {selected[1]}"

def handle_upload(files, current_path):
    if not files: return current_path, None, "无文件"
    for f in files: shutil.move(f.name, os.path.join(current_path, os.path.basename(f.name)))
    items, _ = get_file_info(current_path)
    return current_path, items, None, "✅ 完成"

def list_files(path):
    i, e = get_file_info(path)
    return path, [] if e else i, None, e or f"刷新: {path}"

# ================= 3. HTML 模板 (API 同步版) =================

HTML_TEMPLATE = """
<!doctype html>
<html style="height:100%; margin:0;">
<head>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css" />
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
    <style>
        body { background: #0b0f19; color: #cdd6f4; height: 100%; margin: 0; display: flex; flex-direction: column; overflow: hidden; font-family: 'Segoe UI', sans-serif; }
        
        #tab-bar { height: 36px; background: #1f2937; display: flex; align-items: center; padding: 0 5px; gap: 4px; border-bottom: 1px solid #374151; user-select: none; z-index: 10; position: relative;}
        .tab { padding: 0 10px; height: 28px; background: #374151; border-radius: 4px 4px 0 0; display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 13px; color: #9ca3af; transition: 0.2s;}
        .tab:hover { background: #4b5563; }
        .tab.active { background: #0b0f19; color: #fff; font-weight: bold; border-top: 2px solid #3b82f6; }
        .tab-close { width: 16px; height: 16px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 10px; opacity: 0.6; }
        .tab-close:hover { background: #ef4444; color: white; opacity: 1; }
        
        #add-tab-btn { width: 30px; height: 28px; display: flex; align-items: center; justify-content: center; font-size: 20px; cursor: pointer; color: #9ca3af; border-radius: 4px; background: #252f3e; transition: 0.2s;}
        #add-tab-btn:hover { background: #3b82f6; color: white; }
        #add-tab-btn:active { transform: scale(0.95); }

        #terminals-wrapper { flex: 1; position: relative; overflow: hidden; min-height: 0; z-index: 1;}
        .terminal-instance { width: 100%; height: 100%; display: none; padding: 5px; box-sizing: border-box; }
        .terminal-instance.active { display: block; }
        
        #controls { height: 48px; flex-shrink: 0; display: flex; align-items: center; padding: 0 12px; background: #181c29; border-top: 1px solid #2b303b; gap: 10px; z-index: 10;}
        input { flex: 1; padding: 8px 12px; border-radius: 6px; border: 1px solid #333; background: #0b0f19; color: #4ade80; outline: none; font-family: monospace; }
        button { padding: 7px 16px; border-radius: 6px; cursor: pointer; border: none; color: white; font-weight: 600; white-space: nowrap; }
        #btn-send { background: #3b82f6; } 
        #btn-restart { background: #ef4444; }
        
        #loading { position: absolute; top:0; left:0; width:100%; height:100%; background: #0b0f19; color: #666; display: flex; justify-content: center; align-items: center; z-index: 100; }
    </style>
</head>
<body>
    <div id="loading">Syncing with Server...</div>

    <div id="tab-bar">
        <div id="add-tab-btn" title="Add Terminal">+</div>
    </div>
    <div id="terminals-wrapper"></div>
    <div id="controls">
        <span style="color:#6b7280; font-weight:bold; margin-right:5px;">$</span>
        <input type="text" id="cmd-input" placeholder="Enter command..." autocomplete="off"/>
        <button id="btn-send">Run</button>
        <div style="width:1px;height:20px;background:#333;margin:0 4px;"></div>
        <button id="btn-restart">Reset Process</button>
    </div>

    <script>
        let tabs = {}; 
        let activeTabId = null;
        const MAX_TABS = 5;

        function waitForXterm() {
            if (typeof Terminal !== 'undefined' && typeof FitAddon !== 'undefined') {
                document.getElementById('loading').style.display = 'none';
                initApp();
            } else { setTimeout(waitForXterm, 100); }
        }
        
        async function initApp() {
            document.getElementById('add-tab-btn').addEventListener('click', addNextTab);
            document.getElementById('btn-send').addEventListener('click', sendCommand);
            document.getElementById('btn-restart').addEventListener('click', resetProcess);
            const input = document.getElementById('cmd-input');
            input.addEventListener('keydown', (e) => { if(e.key==='Enter') sendCommand(); });
            
            // --- 核心改动：从服务器获取活跃会话 ---
            try {
                const resp = await fetch('/api/active_sessions');
                const data = await resp.json();
                
                if (data.sessions && data.sessions.length > 0) {
                    console.log("Restoring sessions from server:", data.sessions);
                    // 按照 ID 排序，保证顺序
                    data.sessions.sort().forEach(id => {
                        // 提取数字作为名称
                        const num = id.split('-')[1];
                        createTabInstance(id, `Terminal ${num}`);
                    });
                } else {
                    // 服务器是空的，创建第一个
                    createTabInstance("term-1", "Terminal 1");
                }
            } catch (e) {
                console.error("Failed to sync:", e);
                createTabInstance("term-1", "Terminal 1 (Offline)");
            }
            
            setTimeout(() => input.focus(), 500);
        }

        function getNextTabName() {
            for (let i = 1; i <= MAX_TABS; i++) {
                const id = `term-${i}`;
                if (!tabs[id]) return { id, name: `Terminal ${i}` };
            }
            return null;
        }

        function addNextTab() {
            const next = getNextTabName();
            if (!next) { alert("Max 5 tabs."); return; }
            createTabInstance(next.id, next.name);
        }

        function createTabInstance(id, name) {
            // UI
            const tabBtn = document.createElement('div');
            tabBtn.className = 'tab';
            tabBtn.innerHTML = `<span>${name}</span><div class="tab-close">✕</div>`;
            
            tabBtn.addEventListener('click', (e) => { if(!e.target.classList.contains('tab-close')) switchTab(id); });
            tabBtn.querySelector('.tab-close').addEventListener('click', (e) => { e.stopPropagation(); closeTab(id); });

            const addBtn = document.getElementById('add-tab-btn');
            document.getElementById('tab-bar').insertBefore(tabBtn, addBtn);

            const termContainer = document.createElement('div');
            termContainer.className = 'terminal-instance';
            termContainer.id = id;
            document.getElementById('terminals-wrapper').appendChild(termContainer);

            // Xterm
            const term = new Terminal({
                cursorBlink:true, fontSize:14, fontFamily:'Menlo, monospace', 
                theme:{background:'#0b0f19', foreground:'#cdd6f4'}, allowProposedApi:true
            });
            const fitAddon = new FitAddon.FitAddon();
            term.loadAddon(fitAddon);
            term.open(termContainer);

            // WebSocket
            const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
            const wsUrl = `${protocol}${window.location.host}/ws/${id}`;
            const ws = new WebSocket(wsUrl);

            ws.onopen = () => { 
                setTimeout(() => {
                    fitAddon.fit();
                    ws.send(JSON.stringify({type:"resize", rows:term.rows, cols:term.cols}));
                }, 100);
            };
            ws.onmessage = e => term.write(e.data);
            ws.onclose = () => {
                term.write('\\r\\n\\x1b[31m[Disconnected]\\x1b[0m');
                // 3秒后尝试重连
                setTimeout(() => { if(tabs[id]) { term.write('\\r\\n\\x1b[33m[Reload to reconnect]\\x1b[0m'); } }, 3000);
            };
            term.onData(d => { if(ws.readyState===1) ws.send(JSON.stringify({type:"input", data:d})); });

            tabs[id] = { term, ws, fitAddon, element: termContainer, tabBtn };
            switchTab(id);
        }

        async function closeTab(id) {
            if (!tabs[id]) return;
            
            // 1. 关闭前端连接
            tabs[id].ws.close();
            tabs[id].element.remove();
            tabs[id].tabBtn.remove();
            delete tabs[id];

            if (activeTabId === id) {
                const keys = Object.keys(tabs);
                if (keys.length > 0) switchTab(keys[keys.length - 1]);
                else activeTabId = null;
            }

            // 2. 通知服务器销毁该会话 (真正杀进程)
            try {
                await fetch(`/api/close_session?id=${id}`, { method: 'POST' });
            } catch(e) { console.error(e); }
        }

        function switchTab(id) {
            if (activeTabId && tabs[activeTabId]) {
                tabs[activeTabId].element.classList.remove('active');
                tabs[activeTabId].tabBtn.classList.remove('active');
            }
            activeTabId = id;
            tabs[id].element.classList.add('active');
            tabs[id].tabBtn.classList.add('active');
            setTimeout(() => {
                tabs[id].fitAddon.fit();
                tabs[id].term.focus();
                if(tabs[id].ws.readyState === 1) {
                    tabs[id].ws.send(JSON.stringify({type:"resize", rows:tabs[id].term.rows, cols:tabs[id].term.cols}));
                }
            }, 50);
        }

        function sendCommand() {
            const input = document.getElementById('cmd-input');
            if (!activeTabId) return;
            const ws = tabs[activeTabId].ws;
            if (ws.readyState === 1) {
                ws.send(JSON.stringify({type:"input", data: input.value + "\\n"}));
                input.value = '';
            }
        }
        
        function resetProcess() {
            if (!activeTabId) return;
            if(confirm("Dangerous: Reset this process?")) {
                 const ws = tabs[activeTabId].ws;
                 if (ws.readyState === 1) ws.send(JSON.stringify({type:"restart"}));
            }
        }

        window.onresize = () => { if(activeTabId && tabs[activeTabId]) tabs[activeTabId].fitAddon.fit(); };
        waitForXterm();
    </script>
</body>
</html>
"""

# ================= 4. 路由处理 =================

async def terminal_ui_handler(request: Request):
    return HTMLResponse(content=HTML_TEMPLATE)

# API: 获取所有活跃会话
async def api_get_sessions(request: Request):
    # 返回排序后的 Session ID
    active_ids = sorted(list(SESSIONS.keys()))
    return JSONResponse({"sessions": active_ids})

# API: 显式关闭会话
async def api_close_session(request: Request):
    session_id = request.query_params.get("id")
    if session_id:
        print(f"API Request: Closing session {session_id}")
        remove_session(session_id)
    return JSONResponse({"status": "ok"})

async def websocket_handler(ws: WebSocket, session_id: str):
    await ws.accept()
    session = get_session(session_id)
    if not session.running: session.start()
    await session.connect(ws)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg['type'] == 'input': session.write(msg['data'])
            elif msg['type'] == 'resize': session.resize(msg['rows'], msg['cols'])
            elif msg['type'] == 'restart': session.restart()
    except WebSocketDisconnect:
        session.disconnect(ws)
    except:
        session.disconnect(ws)

# ================= 5. Hook =================

_orig_create_app = gradio.routes.App.create_app
def _hooked_create_app(self, *args, **kwargs):
    app = _orig_create_app(self, *args, **kwargs)
    app.add_api_route("/terminal_ui", terminal_ui_handler, methods=["GET"])
    app.add_api_websocket_route("/ws/{session_id}", websocket_handler)
    
    # 新增 API 路由
    app.add_api_route("/api/active_sessions", api_get_sessions, methods=["GET"])
    app.add_api_route("/api/close_session", api_close_session, methods=["POST"])
    
    return app
gradio.routes.App.create_app = _hooked_create_app

# ================= 6. Gradio UI =================

with gr.Blocks(title="Persistent Terminal", css=".gradio-container {max_width: 100% !important;}") as demo:
    gr.Markdown("## 🛠️ Server Dashboard")
    
    with gr.Tabs():
        with gr.Tab("💻 Terminal"):
            gr.HTML(
                """
                <div style="border: 1px solid #374151; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.3);">
                    <iframe src="/terminal_ui" style="width: 100%; height: 75vh; border: none; display: block;"></iframe>
                </div>
                """
            )
            gr.Markdown("> **提示**: 标签页状态由服务器同步。刷新浏览器会重新加载所有后台运行的终端。")

        with gr.Tab("📂 Files"):
            curr_path = gr.State(os.getcwd())
            with gr.Row():
                p_in = gr.Textbox(label="Path", value=os.getcwd(), scale=4)
                go = gr.Button("Go", scale=1); up = gr.Button("Up", scale=1)
            ft = gr.Dataframe(headers=["T", "Name", "Size", "Date", "Path"], interactive=False, col_count=(5,"fixed"))
            with gr.Row():
                dl = gr.File(label="Download", interactive=False)
                ul = gr.File(label="Upload", file_count="multiple")
            msg = gr.Textbox(label="Msg", value="Ready")

            demo.load(list_files, [curr_path], [p_in, ft, dl, msg])
            go.click(list_files, [p_in], [p_in, ft, dl, msg]).then(lambda x:x, [p_in], [curr_path])
            up.click(lambda x: list_files(os.path.dirname(x)), [curr_path], [p_in, ft, dl, msg]).then(os.path.dirname, [curr_path], [curr_path])
            ft.select(handle_open, [curr_path, ft], [p_in, ft, dl, msg]).then(lambda x:x, [p_in], [curr_path])
            ul.upload(handle_upload, [ul, curr_path], [p_in, ft, dl, msg])

if __name__ == "__main__":
    demo.launch()