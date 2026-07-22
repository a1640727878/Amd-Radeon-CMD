import gradio as gr
import subprocess
import os
import signal
import sys
import platform


class Terminal:
    """模拟一个持久化的终端会话"""

    def __init__(self):
        self.cwd = os.path.expanduser("~")
        self.env = os.environ.copy()
        self.history = []
        self.username = os.getenv("USER") or os.getenv("USERNAME") or "user"
        self.hostname = platform.node() or "localhost"

    def get_prompt(self):
        """生成类似真实终端的提示符"""
        # 将 home 目录替换为 ~
        display_cwd = self.cwd.replace(os.path.expanduser("~"), "~")
        return f"\033[32m{self.username}@{self.hostname}\033[0m:\033[34m{display_cwd}\033[0m$ "

    def execute(self, command: str) -> str:
        """执行命令并返回输出"""
        command = command.strip()

        if not command:
            return ""

        self.history.append(command)

        # 处理内置命令
        result = self._handle_builtin(command)
        if result is not None:
            return result

        # 执行外部命令
        return self._run_command(command)

    def _handle_builtin(self, command: str):
        """处理内置命令 (cd, clear, exit 等)"""

        parts = command.split()
        cmd = parts[0]

        # cd 命令
        if cmd == "cd":
            return self._cmd_cd(parts)

        # clear 命令
        if cmd in ("clear", "cls"):
            return "__CLEAR__"

        # exit 命令
        if cmd == "exit":
            return "👋 Terminal session ended. Refresh to start a new one."

        # history 命令
        if cmd == "history":
            return self._cmd_history()

        # pwd 命令
        if cmd == "pwd":
            return self.cwd

        # export 命令 (设置环境变量)
        if cmd == "export" and len(parts) > 1:
            return self._cmd_export(parts[1:])

        return None  # 非内置命令

    def _cmd_cd(self, parts):
        """处理 cd 命令"""
        if len(parts) == 1:
            target = os.path.expanduser("~")
        elif parts[1] == "-":
            target = self.env.get("OLDPWD", self.cwd)
        else:
            target = parts[1]
            if target.startswith("~"):
                target = os.path.expanduser(target)

        # 处理相对路径
        if not os.path.isabs(target):
            target = os.path.join(self.cwd, target)

        target = os.path.normpath(target)

        if os.path.isdir(target):
            self.env["OLDPWD"] = self.cwd
            self.cwd = target
            self.env["PWD"] = self.cwd
            return ""
        else:
            return f"cd: no such file or directory: {parts[1]}"

    def _cmd_history(self):
        """显示命令历史"""
        lines = []
        for i, cmd in enumerate(self.history, 1):
            lines.append(f"  {i:4d}  {cmd}")
        return "\n".join(lines)

    def _cmd_export(self, args):
        """处理 export 命令"""
        for arg in args:
            if "=" in arg:
                key, value = arg.split("=", 1)
                self.env[key] = value
            else:
                return f"export: invalid syntax: {arg}"
        return ""

    def _run_command(self, command: str) -> str:
        """运行外部命令"""
        try:
            # 判断操作系统选择 shell
            if platform.system() == "Windows":
                shell_cmd = ["cmd", "/c", command]
            else:
                shell_cmd = ["/bin/bash", "-c", command]

            process = subprocess.run(
                shell_cmd,
                capture_output=True,
                text=True,
                cwd=self.cwd,
                env=self.env,
                timeout=30,  # 30秒超时
            )

            output = ""
            if process.stdout:
                output += process.stdout
            if process.stderr:
                output += process.stderr

            # 去除末尾多余的换行
            return output.rstrip("\n")

        except subprocess.TimeoutExpired:
            return "❌ Error: Command timed out (30s limit)"
        except FileNotFoundError:
            return f"bash: {command.split()[0]}: command not found"
        except PermissionError:
            return f"bash: {command.split()[0]}: Permission denied"
        except Exception as e:
            return f"❌ Error: {str(e)}"


def create_app():
    """创建 Gradio 应用"""

    terminal = Terminal()

    # 构建欢迎信息
    welcome_message = f"""Welcome to Gradio Terminal 🖥️
System: {platform.system()} {platform.release()} ({platform.machine()})
User: {terminal.username}@{terminal.hostname}
Home: {os.path.expanduser("~")}
Python: {sys.version.split()[0]}
{'─' * 60}
Type 'help' for available commands.

{terminal.get_prompt()}"""

    # 自定义 CSS
    custom_css = """
    #terminal-output {
        background-color: #1e1e1e !important;
        color: #d4d4d4 !important;
        font-family: 'Cascadia Code', 'Fira Code', 'Consolas', 'Monaco', 'Courier New', monospace !important;
        font-size: 14px !important;
        padding: 16px !important;
        border-radius: 8px !important;
        min-height: 500px !important;
        max-height: 700px !important;
        overflow-y: auto !important;
        white-space: pre-wrap !important;
        word-wrap: break-word !important;
        line-height: 1.5 !important;
        border: 2px solid #333 !important;
    }
    #terminal-output textarea {
        background-color: #1e1e1e !important;
        color: #d4d4d4 !important;
        font-family: 'Cascadia Code', 'Fira Code', 'Consolas', 'Monaco', 'Courier New', monospace !important;
        font-size: 14px !important;
        line-height: 1.5 !important;
    }
    #command-input {
        font-family: 'Cascadia Code', 'Fira Code', 'Consolas', 'Monaco', 'Courier New', monospace !important;
        font-size: 14px !important;
    }
    #command-input input {
        background-color: #2d2d2d !important;
        color: #00ff00 !important;
        font-family: 'Cascadia Code', 'Fira Code', 'Consolas', 'Monaco', 'Courier New', monospace !important;
        font-size: 14px !important;
        border: 2px solid #444 !important;
        border-radius: 4px !important;
    }
    #run-btn {
        background-color: #0e639c !important;
        font-family: monospace !important;
        min-width: 100px !important;
    }
    #clear-btn, #reset-btn {
        font-family: monospace !important;
    }
    .title-text {
        font-family: monospace !important;
    }
    footer {
        display: none !important;
    }
    """

    with gr.Blocks(
        css=custom_css,
        title="Gradio Terminal",
        theme=gr.themes.Soft(primary_hue="blue"),
    ) as app:

        # 标题
        gr.HTML(
            """
            <div style="text-align: center; padding: 10px 0;">
                <h1 style="font-family: monospace; color: #00ff00; margin: 0;">
                    🖥️ Gradio Web Terminal
                </h1>
                <p style="font-family: monospace; color: #888; margin: 5px 0 0 0;">
                    A web-based terminal emulator powered by Gradio
                </p>
            </div>
            """
        )

        # 终端输出区域
        output_box = gr.Textbox(
            value=welcome_message,
            label="Terminal Output",
            lines=25,
            max_lines=50,
            interactive=False,
            elem_id="terminal-output",
        )

        # 输入区域
        with gr.Row():
            command_input = gr.Textbox(
                label="Command",
                placeholder="Enter command here... (Press Enter to execute)",
                scale=5,
                elem_id="command-input",
            )
            run_btn = gr.Button(
                "▶ Run",
                variant="primary",
                scale=1,
                elem_id="run-btn",
            )

        # 功能按钮
        with gr.Row():
            clear_btn = gr.Button("🗑️ Clear Screen", elem_id="clear-btn")
            reset_btn = gr.Button("🔄 Reset Terminal", elem_id="reset-btn")

        # 快捷命令
        with gr.Accordion("⚡ Quick Commands", open=False):
            with gr.Row():
                gr.Button("📂 ls -la").click(
                    lambda h: execute_quick(terminal, "ls -la", h),
                    inputs=[output_box],
                    outputs=[output_box, command_input],
                )
                gr.Button("📍 pwd").click(
                    lambda h: execute_quick(terminal, "pwd", h),
                    inputs=[output_box],
                    outputs=[output_box, command_input],
                )
                gr.Button("💻 uname -a").click(
                    lambda h: execute_quick(terminal, "uname -a", h),
                    inputs=[output_box],
                    outputs=[output_box, command_input],
                )
                gr.Button("📊 df -h").click(
                    lambda h: execute_quick(terminal, "df -h", h),
                    inputs=[output_box],
                    outputs=[output_box, command_input],
                )
            with gr.Row():
                gr.Button("🔍 ps aux").click(
                    lambda h: execute_quick(terminal, "ps aux", h),
                    inputs=[output_box],
                    outputs=[output_box, command_input],
                )
                gr.Button("🌐 ip addr").click(
                    lambda h: execute_quick(terminal, "ip addr 2>/dev/null || ifconfig", h),
                    inputs=[output_box],
                    outputs=[output_box, command_input],
                )
                gr.Button("📅 date").click(
                    lambda h: execute_quick(terminal, "date", h),
                    inputs=[output_box],
                    outputs=[output_box, command_input],
                )
                gr.Button("⏱️ uptime").click(
                    lambda h: execute_quick(terminal, "uptime", h),
                    inputs=[output_box],
                    outputs=[output_box, command_input],
                )

        # =============== 事件处理函数 ===============

        def run_command(command, current_output):
            """执行命令并更新输出"""
            if not command.strip():
                return current_output, ""

            cmd = command.strip()

            # help 命令
            if cmd == "help":
                help_text = """
Available built-in commands:
  cd [dir]       Change directory
  pwd            Print working directory
  clear/cls      Clear terminal screen
  history        Show command history
  export K=V     Set environment variable
  exit           End session
  help           Show this help

All other commands are executed via system shell.
Timeout: 30 seconds per command.
"""
                result = help_text.strip()
            else:
                result = terminal.execute(cmd)

            # 处理 clear 命令
            if result == "__CLEAR__":
                new_output = terminal.get_prompt()
                return new_output, ""

            # 构建新的输出
            display_cmd = f"{command}"
            if result:
                new_output = f"{current_output}{display_cmd}\n{result}\n\n{terminal.get_prompt()}"
            else:
                new_output = f"{current_output}{display_cmd}\n\n{terminal.get_prompt()}"

            return new_output, ""

        def clear_screen():
            """清屏"""
            return terminal.get_prompt()

        def reset_terminal():
            """重置终端"""
            nonlocal terminal
            terminal = Terminal()
            return welcome_message + "\n", ""

        # 绑定事件
        # 按下 Enter 执行命令
        command_input.submit(
            fn=run_command,
            inputs=[command_input, output_box],
            outputs=[output_box, command_input],
        )

        # 点击 Run 按钮执行命令
        run_btn.click(
            fn=run_command,
            inputs=[command_input, output_box],
            outputs=[output_box, command_input],
        )

        # 清屏按钮
        clear_btn.click(
            fn=clear_screen,
            outputs=[output_box],
        )

        # 重置按钮
        reset_btn.click(
            fn=reset_terminal,
            outputs=[output_box, command_input],
        )

    return app


def execute_quick(terminal, command, current_output):
    """执行快捷命令"""
    result = terminal.execute(command)

    if result == "__CLEAR__":
        return terminal.get_prompt(), ""

    if result:
        new_output = f"{current_output}{command}\n{result}\n\n{terminal.get_prompt()}"
    else:
        new_output = f"{current_output}{command}\n\n{terminal.get_prompt()}"

    return new_output, ""


# =============== 主入口 ===============

if __name__ == "__main__":
    app = create_app()
    app.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
    )