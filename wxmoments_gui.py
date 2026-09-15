from __future__ import annotations

import asyncio
import contextlib
import html
import json
import multiprocessing
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, END, StringVar, Tk, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


APP_NAME = "微信朋友圈导出工具1.0"
PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
SRC_ROOT = PROJECT_ROOT / "src"
if SRC_ROOT.exists():
    sys.path.insert(0, str(SRC_ROOT))

if getattr(sys, "frozen", False):
    app_data = Path(os.environ.get("APPDATA", Path.home())) / "wxMoments"
    os.environ.setdefault("WXMOMENTS_APP_DATA_DIR", str(app_data))

import wxmoments  # noqa: E402

try:
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError:  # pragma: no cover - only relevant on non-Windows development hosts
    win32api = win32con = win32gui = win32process = None


PHOTO_CELL_RE = re.compile(
    r'(?P<open><div class="image-cell"[^>]*>)'
    r'(?P<img><img\s+src="(?P<src>[^"]+)"[^>]*>)'
    r'(?P<close></div>)'
)


def make_photos_clickable(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if ".photo-link" not in text:
        text = text.replace(
            ".image-cell img {",
            ".photo-link { display:block; width:100%; height:100%; }\n.image-cell img {",
            1,
        )

    def wrap(match: re.Match[str]) -> str:
        if "photo-link" in match.group(0):
            return match.group(0)
        src = match.group("src")
        return (
            f'{match.group("open")}<a class="photo-link" href="{src}" '
            f'target="_blank" rel="noopener">{match.group("img")}</a>{match.group("close")}'
        )

    updated, count = PHOTO_CELL_RE.subn(wrap, text)
    if updated != path.read_text(encoding="utf-8"):
        path.write_text(updated, encoding="utf-8")
    return count


def make_index(root: Path, rows: list[dict[str, object]]) -> Path:
    table_rows = []
    for row in rows:
        year = int(row["year"])
        table_rows.append(
            f'<tr><td><a href="{year}/朋友圈_{year}.html">{year}</a></td>'
            f'<td>{int(row.get("posts", 0))}</td><td>{int(row.get("images", 0))}</td></tr>'
        )
    content = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>微信朋友圈年度索引</title>
<style>body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#222}table{border-collapse:collapse;width:100%;margin-top:20px}th,td{border:1px solid #ddd;padding:10px;text-align:left}th{background:#f5f5f5}a{color:#1769aa;text-decoration:none}a:hover{text-decoration:underline}</style>
</head><body><h1>微信朋友圈年度索引</h1><p>每年独立导出；点击年份进入该年度朋友圈，点击照片可打开原图。</p>
<table><thead><tr><th>年份</th><th>朋友圈条数</th><th>可点击照片</th></tr></thead><tbody>
""" + "\n".join(table_rows) + """
</tbody></table></body></html>
"""
    index = root / "年度索引.html"
    index.write_text(content, encoding="utf-8")
    return index


class ExportApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("780x680")
        self.root.minsize(720, 580)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.export_stop = threading.Event()
        self.current_output: Path | None = None
        self.refresh_worker: threading.Thread | None = None
        self.refresh_stop = threading.Event()

        config = wxmoments.load_config(wxmoments.DEFAULT_CONFIG)
        default_output = str(Path.home() / "Desktop" / "朋友圈导出")
        if not Path(default_output).parent.exists():
            default_output = str(Path.home() / "朋友圈导出")

        self.output_var = StringVar(value=default_output)
        self.first_year_var = StringVar(value="2012")
        self.last_year_var = StringVar(value=str(datetime.now().year))
        self.only_self_var = BooleanVar(value=True)
        self.interactions_var = BooleanVar(value=True)
        self.allow_download_var = BooleanVar(value=False)
        self.refresh_interval_var = StringVar(value="1.0")
        self.refresh_status_var = StringVar(value="未启动")
        self.phase_var = StringVar(value="当前阶段：准备就绪")
        self.status_var = StringVar(value="准备就绪")
        self.progress_text_var = StringVar(value="0%")
        self.latest_var = StringVar(value="最新导出：暂无")
        ttk.Style(root).configure("Export.Horizontal.TProgressbar", thickness=18)
        self.progress: ttk.Progressbar | None = None

        self._build_ui()
        self.root.after(150, self._drain_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_NAME, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="按年份独立导出 HTML，照片可点击打开。请先在电脑版微信中加载需要的朋友圈缓存。", foreground="#555").pack(anchor="w", pady=(5, 18))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        self._path_row(form, 0, "输出目录", self.output_var, self._choose_output)
        ttk.Label(form, text="微信数据目录和数据库密钥将在导出时自动定位和获取", foreground="#777").grid(row=1, column=1, columnspan=3, sticky="w", pady=(0, 7))

        ttk.Label(form, text="年份范围").grid(row=2, column=0, sticky="w", pady=7)
        year_frame = ttk.Frame(form)
        year_frame.grid(row=2, column=1, sticky="w", pady=7)
        ttk.Entry(year_frame, width=8, textvariable=self.first_year_var).pack(side="left")
        ttk.Label(year_frame, text="  至  ").pack(side="left")
        ttk.Entry(year_frame, width=8, textvariable=self.last_year_var).pack(side="left")

        options = ttk.LabelFrame(outer, text="导出选项", padding=12)
        options.pack(fill="x", pady=(16, 12))
        ttk.Checkbutton(options, text="只导出自己的朋友圈", variable=self.only_self_var).pack(side="left", padx=(0, 22))
        ttk.Checkbutton(options, text="保留点赞和评论", variable=self.interactions_var).pack(side="left", padx=(0, 22))
        ttk.Checkbutton(options, text="允许联网补全缺失图片", variable=self.allow_download_var).pack(side="left")

        helper = ttk.LabelFrame(outer, text="朋友圈辅助刷新", padding=12)
        helper.pack(fill="x", pady=(0, 12))
        ttk.Label(helper, text="先切换到微信朋友圈界面，并点击左上角切换到最早的一条；然后按 F9 开始，再次按 F9 结束。", foreground="#8a4b08", wraplength=680).grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 8))
        ttk.Label(helper, text="间隔（秒）").grid(row=1, column=0, sticky="w")
        ttk.Entry(helper, width=7, textvariable=self.refresh_interval_var).grid(row=1, column=1, sticky="w", padx=(6, 14))
        self.refresh_button = ttk.Button(helper, text="启用 F9 辅助刷新", command=self.start_refresh)
        self.refresh_button.grid(row=1, column=2, sticky="w")
        self.stop_refresh_button = ttk.Button(helper, text="退出辅助刷新", command=self.stop_refresh, state="disabled")
        self.stop_refresh_button.grid(row=1, column=3, sticky="w", padx=(8, 0))
        ttk.Label(helper, textvariable=self.refresh_status_var, foreground="#1769aa").grid(row=1, column=4, sticky="w", padx=(14, 0))
        ttk.Label(helper, text="F9：开始 / 结束", foreground="#777").grid(row=1, column=5, sticky="w", padx=(14, 0))

        action = ttk.Frame(outer)
        action.pack(fill="x", pady=(0, 10))
        self.start_button = ttk.Button(action, text="开始逐年导出", command=self.start_export)
        self.start_button.pack(side="left")
        self.stop_export_button = ttk.Button(action, text="停止导出", command=self.stop_export, state="disabled")
        self.stop_export_button.pack(side="left", padx=(10, 0))
        self.open_button = ttk.Button(action, text="打开上次输出目录", command=self.open_last_output, state="disabled")
        self.open_button.pack(side="left", padx=10)
        progress_frame = ttk.Frame(outer)
        progress_frame.pack(fill="x", pady=(0, 6))
        self.progress = ttk.Progressbar(
            progress_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            value=0,
            length=640,
            style="Export.Horizontal.TProgressbar",
        )
        self.progress.pack(side="left", fill="x", expand=True, ipady=3)
        ttk.Label(progress_frame, textvariable=self.progress_text_var, width=7, anchor="e").pack(side="right", padx=(10, 0))
        ttk.Label(outer, textvariable=self.phase_var, foreground="#1769aa", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(outer, textvariable=self.status_var, foreground="#1769aa").pack(anchor="w", pady=(2, 0))
        ttk.Label(outer, textvariable=self.latest_var, foreground="#555", wraplength=700).pack(anchor="w", pady=(2, 0))

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log = ScrolledText(log_frame, height=16, wrap="word", state="disabled", font=("Consolas", 10))
        self.log.pack(fill="both", expand=True)

        for column in (1, 2):
            form.columnconfigure(column, weight=1)

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: StringVar, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=7)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky="ew", pady=7)
        ttk.Button(parent, text="浏览…", command=command).grid(row=row, column=3, padx=(8, 0), pady=7)

    def _choose_output(self) -> None:
        chosen = filedialog.askdirectory(title="选择输出目录")
        if chosen:
            self.output_var.set(chosen)

    def _wechat_window(self):
        if not all((win32api, win32con, win32gui, win32process)):
            raise RuntimeError("当前系统缺少 Windows 键盘控制组件，请使用 Windows 版本 EXE。")
        found: list[tuple[int, int]] = []

        def callback(hwnd: int, _extra: object) -> None:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetParent(hwnd):
                return
            try:
                _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
                process = __import__("psutil").Process(pid)
                name = str(process.name() or "").lower()
                title = str(win32gui.GetWindowText(hwnd) or "").strip()
                if name in {"weixin.exe", "wechat.exe"} and title:
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    found.append((max(0, (right - left) * (bottom - top)), hwnd))
            except Exception:
                return

        win32gui.EnumWindows(callback, None)
        if not found:
            raise RuntimeError("没有找到正在运行的电脑版微信窗口，请先打开并登录微信。")
        return max(found)[1]

    def _foreground_wechat_window(self):
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        try:
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = str(__import__("psutil").Process(pid).name() or "").lower()
            if name in {"weixin.exe", "wechat.exe"}:
                return hwnd
        except Exception:
            pass
        return None

    def start_refresh(self) -> None:
        if self.refresh_worker and self.refresh_worker.is_alive():
            return
        try:
            interval = float(self.refresh_interval_var.get().strip())
            if not (0.2 <= interval <= 60):
                raise ValueError
            self._wechat_window()
        except ValueError:
            messagebox.showerror("参数不正确", "间隔应为 0.2 到 60 秒。")
            return
        except Exception as exc:
            messagebox.showerror("无法启动辅助刷新", str(exc))
            return
        if not messagebox.askokcancel(
            "开始辅助刷新",
            "请先切换到微信朋友圈界面，并点击左上角切换到最早的一条。\n\n点击“确定”后，请切换回微信朋友圈窗口，再按 F9 开始；再次按 F9 结束。",
        ):
            return
        self.refresh_stop.clear()
        self.refresh_button.configure(state="disabled")
        self.stop_refresh_button.configure(state="normal")
        self.refresh_status_var.set("等待 F9")
        self._append_log("已启用 F9 辅助刷新：请切换到微信朋友圈窗口，按 F9 开始；再次按 F9 结束。")
        self.refresh_worker = threading.Thread(
            target=self._refresh_loop,
            args=(interval,),
            daemon=True,
        )
        self.refresh_worker.start()

    def stop_refresh(self) -> None:
        self.refresh_stop.set()
        self.refresh_status_var.set("正在停止…")

    def _refresh_loop(self, interval: float) -> None:
        try:
            active = False
            hwnd = None
            f9_down = False
            next_press = 0.0
            count = 0
            while not self.refresh_stop.is_set():
                pressed = bool(win32api.GetAsyncKeyState(win32con.VK_F9) & 0x8000)
                if pressed and not f9_down:
                    if active:
                        active = False
                        self.events.put(("refresh", "已按 F9 结束辅助刷新"))
                    else:
                        hwnd = self._foreground_wechat_window()
                        if hwnd is None:
                            self.events.put(("refresh", "F9 未启动：请先切换到微信朋友圈窗口，再按 F9"))
                        else:
                            active = True
                            count = 0
                            next_press = time.monotonic()
                            self.events.put(("refresh", "已按 F9 开始辅助刷新，微信窗口必须保持在前台"))
                f9_down = pressed

                if active and time.monotonic() >= next_press:
                    if win32gui.GetForegroundWindow() != hwnd:
                        active = False
                        self.events.put(("refresh", "微信窗口已不在前台，辅助刷新已暂停；切回微信后按 F9 重新开始"))
                    else:
                        win32api.keybd_event(win32con.VK_PRIOR, 0, 0, 0)
                        win32api.keybd_event(win32con.VK_PRIOR, 0, win32con.KEYEVENTF_KEYUP, 0)
                        count += 1
                        next_press = time.monotonic() + interval
                        self.events.put(("refresh_progress", f"辅助刷新中：已发送 PgUp {count} 次"))
                time.sleep(0.03)
        except Exception as exc:
            self.events.put(("refresh_error", str(exc)))
        finally:
            self.events.put(("refresh_done", ""))

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(END, text.rstrip() + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")

    def start_export(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            first = int(self.first_year_var.get().strip())
            last = int(self.last_year_var.get().strip())
            if first < 2000 or last < first or last > 2100:
                raise ValueError
        except ValueError:
            messagebox.showerror("年份不正确", "请输入有效的起止年份。")
            return
        self.export_stop.clear()
        self.current_output = None
        self.start_button.configure(state="disabled")
        self.stop_export_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.progress.configure(value=0)
        self.progress_text_var.set("0%")
        self.phase_var.set("当前阶段：准备导出")
        self.status_var.set("正在准备导出…")
        self.latest_var.set("最新导出：暂无")
        self._append_log("开始导出，请保持微信登录；大型年份可能需要几分钟。")
        self.worker = threading.Thread(target=self._worker_main, args=(first, last), daemon=True)
        self.worker.start()

    def stop_export(self) -> None:
        if self.worker and self.worker.is_alive():
            self.export_stop.set()
            self.phase_var.set("当前阶段：正在停止")
            self.status_var.set("正在停止导出…")
            self._append_log("已请求停止导出，程序会在当前图片处理完成后停止。")

    def _check_export_stop(self) -> None:
        if self.export_stop.is_set():
            raise wxmoments.ExportCancelled("用户已停止导出")

    def _worker_main(self, first: int, last: int) -> None:
        try:
            output = asyncio.run(self._export(first, last))
            self.events.put(("done", output))
        except wxmoments.ExportCancelled as exc:
            self.events.put(("cancelled", str(exc)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    async def _export(self, first: int, last: int) -> Path:
        config = wxmoments.load_config(wxmoments.DEFAULT_CONFIG)
        config["output_root"] = self.output_var.get().strip()
        config["wechat_data_root"] = ""
        config["account"] = ""
        config["db_key"] = ""
        wxmoments.save_config(wxmoments.DEFAULT_CONFIG, config)

        self.events.put(("phase", "当前阶段：查找微信账号"))
        self.events.put(("progress", {"value": 2, "status": "正在检查微信数据目录…"}))
        self.events.put(("log", "正在自动定位微信账号（优先检查标准目录）…"))
        account = wxmoments.find_account(config, stop_event=self.export_stop)
        self.events.put(("log", f"已定位微信账号：{account.account}"))
        self.events.put(("progress", {"value": 5, "status": f"已找到微信账号：{account.account}"}))
        self._check_export_stop()
        key = wxmoments.load_saved_db_key(account)
        if key:
            self.events.put(("log", "已找到本地保存的数据库密钥，跳过内存扫描。"))
        else:
            self.events.put(("phase", "当前阶段：获取数据库密钥"))
            self.events.put(("progress", {"value": 8, "status": "正在获取数据库密钥，请保持微信登录…"}))
            self.events.put(("log", "正在自动获取数据库密钥…"))
            from wechat_decrypt_tool.modules.key_service import get_db_key_workflow

            try:
                result = get_db_key_workflow(db_storage_path=str(account.db_storage_dir))
            except Exception as exc:
                raise RuntimeError(
                    "自动获取数据库密钥失败。请保持电脑版微信已登录，并关闭其他微信窗口后重试。\n"
                    f"详细原因：{exc}"
                ) from exc
            key = str(result.get("db_key") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ValueError("数据库密钥无效，请粘贴 64 位十六进制密钥。")

        self.events.put(("phase", "当前阶段：解密微信数据库"))
        self.events.put(("progress", {"value": 12, "status": "正在解密微信数据库…"}))
        self.events.put(("log", "正在解密数据库…"))
        account_dir = wxmoments.decrypt_databases(account, key)
        self._check_export_stop()
        wxmoments.save_db_key(account, account_dir, key)
        contacts = wxmoments.load_contact_entries(account_dir)
        wxmoments.write_contact_cache(account_dir, contacts)
        names = wxmoments.build_contact_display_names(contacts)
        usernames = wxmoments.self_username_candidates(account, config) if self.only_self_var.get() else None
        self.events.put(("progress", {"value": 17, "status": "数据库解密完成，正在准备图片密钥…"}))
        self.events.put(("phase", "当前阶段：准备图片密钥"))
        self.events.put(("log", "正在准备图片密钥…"))
        await wxmoments.save_image_keys(account.account, account.wxid_dir, account.db_storage_dir)
        self._check_export_stop()

        base = Path(self.output_var.get().strip()).expanduser()
        output = base / f"wxMoments_{datetime.now():%Y%m%d_%H%M%S}"
        output.mkdir(parents=True, exist_ok=True)
        self.current_output = output
        rows: list[dict[str, object]] = []
        year_count = max(1, last - first + 1)
        export_base = 20.0
        export_span = 78.0
        for year_index, year in enumerate(range(first, last + 1)):
            if self.export_stop.is_set():
                raise wxmoments.ExportCancelled("用户已停止导出")
            year_dir = output / str(year)
            year_dir.mkdir(parents=True, exist_ok=True)
            self.events.put(("phase", "当前阶段：导出朋友圈"))
            year_start_percent = export_base + export_span * year_index / year_count
            self.events.put(("progress", {"value": year_start_percent, "status": f"正在读取 {year} 年朋友圈数据…"}))
            start = wxmoments.parse_datetime(f"{year}-01-01", end_of_day=False)
            end = wxmoments.parse_datetime(f"{year}-12-31", end_of_day=True)

            def on_post_progress(index: int, total: int, time_text: str, display: str, *, _year=year, _index=year_index) -> None:
                ratio = index / max(1, total)
                percent = export_base + export_span * (_index + ratio) / year_count
                self.events.put((
                    "progress",
                    {
                        "value": percent,
                        "status": f"正在导出 {_year} 年：第 {index}/{total} 条",
                        "latest": f"最新导出：{display}　{time_text}",
                    },
                ))

            with (year_dir / "export.log").open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log):
                stats, posts = await wxmoments.export_markdown(
                    account,
                    account_dir,
                    year_dir,
                    start,
                    end,
                    usernames,
                    config,
                    bool(self.interactions_var.get()),
                    names,
                    allow_download=bool(self.allow_download_var.get()),
                    max_posts=0,
                    order="oldest",
                    stop_event=self.export_stop,
                    progress_callback=on_post_progress,
                )
            html_path = wxmoments.write_pdf_html(year_dir, posts, filename=f"朋友圈_{year}.html")
            clickable = make_photos_clickable(html_path)
            row = {"year": year, "html": str(html_path), "clickable_photos": clickable, **stats}
            rows.append(row)
            self.events.put(("log", f"{year} 年完成：{stats['posts']} 条朋友圈，{clickable} 张可点击照片"))
            self.events.put((
                "progress",
                {
                    "value": export_base + export_span * (year_index + 1) / year_count,
                    "status": f"{year} 年导出完成：{stats['posts']} 条朋友圈",
                },
            ))
            (output / "年度统计.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            make_index(output, rows)

        (output / "年度统计.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        index = make_index(output, rows)
        self.events.put(("log", f"年度索引已生成：{index}"))
        self.events.put(("phase", "当前阶段：导出完成"))
        self.events.put(("progress", {"value": 100, "status": "全部年度导出完成"}))
        return output

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(value))
                elif kind == "phase":
                    self.phase_var.set(str(value))
                elif kind == "status":
                    self.status_var.set(str(value))
                elif kind == "progress":
                    payload = value if isinstance(value, dict) else {"value": 0, "status": str(value)}
                    percent = max(0.0, min(100.0, float(payload.get("value") or 0)))
                    self.progress.configure(value=percent)
                    self.progress_text_var.set(f"{percent:.0f}%")
                    if payload.get("status"):
                        self.status_var.set(str(payload["status"]))
                    if payload.get("latest"):
                        self.latest_var.set(str(payload["latest"]))
                elif kind == "done":
                    output = Path(str(value))
                    self.phase_var.set("当前阶段：导出完成")
                    self.progress.configure(value=100)
                    self.progress_text_var.set("100%")
                    self.status_var.set("导出完成")
                    self._append_log(f"全部完成：{output}")
                    self.last_output = output
                    self.open_button.configure(state="normal")
                    self.stop_export_button.configure(state="disabled")
                    messagebox.showinfo("导出完成", f"年度索引已生成：\n{output / '年度索引.html'}")
                elif kind == "cancelled":
                    self.phase_var.set("当前阶段：已停止")
                    self.status_var.set("已停止导出")
                    self._append_log(f"导出已停止：{value}")
                    if self.current_output and self.current_output.exists():
                        self.last_output = self.current_output
                        self.open_button.configure(state="normal")
                    self.stop_export_button.configure(state="disabled")
                elif kind == "error":
                    self.phase_var.set("当前阶段：发生错误")
                    self.status_var.set("导出失败")
                    self._append_log(f"错误：{value}")
                    self.stop_export_button.configure(state="disabled")
                    messagebox.showerror("导出失败", str(value))
                elif kind == "refresh":
                    self._append_log(str(value))
                    self.refresh_status_var.set("已停止")
                elif kind == "refresh_progress":
                    self.refresh_status_var.set(str(value))
                elif kind == "refresh_error":
                    self._append_log(f"辅助刷新错误：{value}")
                    self.refresh_status_var.set("出错")
                    messagebox.showerror("辅助刷新失败", str(value))
                elif kind == "refresh_done":
                    self.refresh_button.configure(state="normal")
                    self.stop_refresh_button.configure(state="disabled")
                    if self.refresh_status_var.get() == "正在停止…":
                        self.refresh_status_var.set("未启动")
        except queue.Empty:
            pass
        if not (self.worker and self.worker.is_alive()):
            self.start_button.configure(state="normal")
        self.root.after(150, self._drain_events)

    def open_last_output(self) -> None:
        output = getattr(self, "last_output", None)
        if output and output.exists() and hasattr(os, "startfile"):
            os.startfile(str(output / "年度索引.html"))


def main() -> int:
    multiprocessing.freeze_support()
    root = Tk()
    root.state("normal")
    root.deiconify()
    root.lift()
    root.focus_force()
    # A packaged GUI process can start behind the current foreground window.
    # Temporarily making it topmost ensures the user can see it immediately.
    root.attributes("-topmost", True)
    root.after(900, lambda: root.attributes("-topmost", False))
    try:
        ttk.Style(root).theme_use("vista")
    except Exception:
        pass
    ExportApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
