"""A small Tk desktop entry point; all job work stays outside the UI thread."""
from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_ROOT = Path(__file__).resolve().parent.parent
REVIEW_LEVELS = {
    "快速抽查": (True, 32),
    "较严抽查": (True, 128),
    "先翻译": (False, 32),
}
STATE_LABELS = {
    "finished": "本轮处理结束", "incomplete": "已保存，仍有内容待处理",
    "stopped": "已安全停止，可继续", "budget_reached": "本轮已保存，可继续",
    "no_progress": "已保存，需要查看待处理问题", "blocked": "需要处理问题后继续",
    "error": "本轮未完成，进度已保留", "busy": "此任务正在其他窗口运行",
}
STAGE_LABELS = {
    "prepare": "准备书籍", "translate": "翻译中", "review": "抽查译文",
    "draft": "生成阅读稿", "finished": "本轮结束",
}


@dataclass(frozen=True)
class ProviderChoices:
    labels: tuple[str, ...]
    names: tuple[str, ...]
    default: str | None
    secrets: tuple[str, ...] = field(default=(), repr=False)
    review_default: str | None = None
    pool_names: tuple[str, ...] = ()

    def name_for(self, label: str) -> str | None:
        return self.names[self.labels.index(label)] if label in self.labels else None

    def label_for(self, name: str | None) -> str:
        return self.labels[self.names.index(name)] if name in self.names else ""

    @property
    def review_labels(self) -> tuple[str, ...]:
        return tuple(label for label, name in zip(self.labels, self.names) if name not in self.pool_names)

    @property
    def review_names(self) -> tuple[str, ...]:
        return tuple(name for name in self.names if name not in self.pool_names)

    def review_name_for(self, label: str) -> str | None:
        name = self.name_for(label)
        return name if name not in self.pool_names else None


def read_provider_choices(path: str | Path) -> ProviderChoices:
    """Read only; display provider/model names and keep credentials out of labels."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("无法读取配置文件，请选择有效的 config.json。") from exc
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        raise ValueError("配置文件中没有可用的模型，请先填写配置文件。")
    providers = data["providers"]
    single_names = tuple(str(k) for k, v in providers.items() if isinstance(v, dict))
    if not single_names:
        raise ValueError("配置文件中没有可用的模型，请先填写配置文件。")
    pools = data.get("provider_pools", {})
    if not isinstance(pools, dict):
        raise ValueError("多路由配置无效，请检查供应商池设置。")
    for name, pool in pools.items():
        if not isinstance(name, str) or not name.strip() or name in providers or not isinstance(pool, dict):
            raise ValueError("多路由配置无效，名称应独立且不能与单个供应商重名。")
        members = pool.get("providers")
        if (not isinstance(members, list) or not 1 <= len(members) <= 16
                or any(not isinstance(member, str) or member not in single_names for member in members)
                or len(set(members)) != len(members)):
            raise ValueError("多路由配置无效，每组应包含1至16个不重复的已有供应商。")
    pool_names = tuple(pools)
    names = single_names + pool_names
    secrets = tuple(v["api_key"] for v in providers.values()
                    if isinstance(v, dict) and isinstance(v.get("api_key"), str)
                    and v["api_key"])
    labels = tuple(safe_message(f"{name}  ·  {providers[name].get('model') or '未填写模型'}", secrets)
                   for name in single_names)
    labels += tuple(safe_message(f"{name}  ·  多路由 / {len(pools[name]['providers'])}个供应商", secrets)
                    for name in pool_names)
    default = data.get("_default") or data.get("default_provider") or providers.get("_default")
    if default not in names:
        default = names[0]
    review_default = data.get("_review_default")
    if review_default not in single_names:
        review_default = None
    return ProviderChoices(labels, names, default, secrets, review_default, pool_names)


def safe_message(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|authorization|token)\s*[:=]\s*)[^\s,;]+", r"\1[已隐藏]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[已隐藏]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[已隐藏]@", text)
    return text[:700]


def validate_inputs(values: dict, choices: ProviderChoices, *, draft_only=False) -> dict:
    """Validate form values without creating directories or opening a job database."""
    pdf_text = str(values.get("pdf", "")).strip()
    out_text = str(values.get("out", "")).strip()
    if not out_text:
        raise ValueError("请选择保存进度和阅读稿的任务目录。")
    out = Path(out_text).expanduser().resolve()
    if out.exists() and not out.is_dir():
        raise ValueError("任务目录应是文件夹，不能是文件。")
    existing = (out / "book.json").is_file()
    if pdf_text:
        pdf = Path(pdf_text).expanduser().resolve()
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
            raise ValueError("请选择一个存在的 PDF 文件。")
    elif not existing:
        raise ValueError("首次运行请选择 PDF；继续时请选择已有任务目录。")
    else:
        pdf = None
    if draft_only and not existing:
        raise ValueError("请先开始翻译并保存进度，再生成阅读稿。")
    pages = str(values.get("pages", "")).strip().replace("，", ",").replace("–", "-")
    if pages:
        # This is the same physical-page syntax accepted by the command-line entry point.
        from .cli import parse_pages
        try:
            parse_pages(pages)
        except (ValueError, TypeError):
            raise ValueError("页码请填 1-20,25 这样的格式；留空处理整本。") from None
    try:
        workers = int(values.get("workers", 32))
    except (TypeError, ValueError):
        raise ValueError("同时处理数量须为 1 到 32 的整数。") from None
    if not 1 <= workers <= 32:
        raise ValueError("同时处理数量须为 1 到 32 的整数。")
    level = str(values.get("review_level", "快速抽查"))
    if level not in REVIEW_LEVELS:
        raise ValueError("请选择复核程度。")
    review, limit = REVIEW_LEVELS[level]
    provider = choices.name_for(str(values.get("provider", "")))
    review_provider = choices.review_name_for(str(values.get("review_provider", "")))
    if not draft_only and provider is None:
        raise ValueError("请选择配置文件中的翻译模型。")
    if not draft_only and review and review_provider is None:
        raise ValueError("请选择配置文件中的复核模型。")
    config = Path(str(values.get("config", ""))).expanduser().resolve()
    if not draft_only and not config.is_file():
        raise ValueError("请选择有效的配置文件。")
    return {
        "pdf": str(pdf) if pdf else None, "out": str(out),
        "config_path": str(config) if config.is_file() else None, "provider": provider,
        "review_provider": review_provider, "workers": workers, "batch_size": workers,
        "pages": pages or None, "review": False if draft_only else review,
        "max_review_elements": limit, "max_tasks": 0 if draft_only else None,
        "draft": True,
    }


def default_runner(options: dict, callback: Callable) -> dict:
    # Import only after Start: the window can explain missing runtime dependencies.
    from .autopilot import AutoOptions, run_job
    return run_job(AutoOptions(**options), callback)


class JobWorker:
    """Queue-only bridge. Worker callbacks never touch Tk widgets."""
    def __init__(self, runner: Callable = default_runner):
        self.runner = runner
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, options: dict) -> None:
        if self.running:
            raise RuntimeError("任务已经在运行。")
        # Do not deliver stale completion events into a later run.
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        self.cancel_event = threading.Event()
        frozen = dict(options, cancel_event=self.cancel_event)

        def callback(event):
            if isinstance(event, dict):
                # Copy data in case a producer reuses its dictionary.
                self.events.put(("progress", copy.deepcopy(event)))
            return not self.cancel_event.is_set()

        def work():
            try:
                result = self.runner(frozen, callback)
                self.events.put(("done", result if isinstance(result, dict) else {}))
            except Exception as exc:
                self.events.put(("error", (type(exc).__name__, str(exc))))

        self.thread = threading.Thread(target=work, name="pdf-translation-job", daemon=False)
        self.thread.start()

    def stop(self) -> None:
        self.cancel_event.set()


def open_directory(path: str | Path) -> None:
    if not str(path).strip():
        raise ValueError("请先选择任务目录。")
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("任务目录尚未建立；开始运行并保存后即可打开。")
    if sys.platform == "win32":
        os.startfile(str(folder))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


class DesktopApp(ttk.Frame):
    def __init__(self, master: tk.Tk, *, job: str | None = None,
                 config_path: str | Path | None = None, runner: Callable = default_runner):
        super().__init__(master, padding=22)
        self.master = master
        self.worker = JobWorker(runner)
        self.choices = ProviderChoices((), (), None)
        self._busy = False
        self._closing = False
        self._after_id = None
        self._last_log = ""
        self.vars = {name: tk.StringVar(master, value=value) for name, value in {
            "pdf": "", "out": str(job or ""), "pages": "",
            "config": str(config_path or APP_ROOT / "config.json"),
            "provider": "", "review_provider": "", "workers": "32",
            "review_level": "快速抽查",
        }.items()}
        self.status = tk.StringVar(master, "选择 PDF 或已有任务目录，然后开始。")
        self.detail = tk.StringVar(master, "进度会自动保存；阅读稿中的待处理内容会保留原页并标记。")
        self._settings: list[tuple[tk.Widget, str]] = []
        master.title("教材 PDF 翻译")
        master.minsize(800, 650)
        master.geometry("900x760")
        self.pack(fill="both", expand=True)
        self._build()
        self.load_config(show_error=False)
        master.protocol("WM_DELETE_WINDOW", self.close)
        self._after_id = master.after(120, self._poll)

    def _setting(self, widget, state="normal"):
        self._settings.append((widget, state))
        return widget

    def _build(self):
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="把教材变成可读的中文阅读稿", font=("Microsoft YaHei UI", 17, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(self, text="保留数学对象 · 自动分批保存 · 可停止后继续").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 18))

        def path_row(row, label, name, command, button):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
            self._setting(ttk.Entry(self, textvariable=self.vars[name])).grid(row=row, column=1, sticky="ew", pady=6)
            self._setting(ttk.Button(self, text=button, command=command)).grid(row=row, column=2, padx=(10, 0), pady=6)

        path_row(2, "原 PDF", "pdf", self.choose_pdf, "选择文件")
        path_row(3, "任务目录", "out", self.choose_job, "选择 / 继续")
        ttk.Label(self, text="继续已有任务时，选原来的任务目录；PDF 可留空。", foreground="#555555").grid(
            row=4, column=1, columnspan=2, sticky="w")
        path_row(5, "模型配置", "config", self.choose_config, "选择配置")
        ttk.Label(self, text="原 PDF 页码").grid(row=6, column=0, sticky="w", pady=7)
        self._setting(ttk.Entry(self, textvariable=self.vars["pages"])).grid(row=6, column=1, sticky="ew", pady=7)
        ttk.Label(self, text="留空整本，例如 1-20,25").grid(row=6, column=2, sticky="w", padx=(10, 0))

        for row, name, label in ((7, "provider", "翻译模型"), (8, "review_provider", "复核模型")):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=7)
            combo = self._setting(ttk.Combobox(self, textvariable=self.vars[name], state="readonly"), "readonly")
            combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=7)
            setattr(self, name + "_combo", combo)
        controls = ttk.Frame(self)
        controls.grid(row=9, column=1, columnspan=2, sticky="w", pady=8)
        ttk.Label(self, text="处理与复核").grid(row=9, column=0, sticky="w")
        self._setting(ttk.Combobox(controls, textvariable=self.vars["review_level"],
            values=tuple(REVIEW_LEVELS), state="readonly", width=12), "readonly").pack(side="left")
        ttk.Label(controls, text="   翻译并发").pack(side="left")
        self.workers_spin = self._setting(ttk.Spinbox(controls, textvariable=self.vars["workers"], from_=1, to=32, width=5))
        self.workers_spin.pack(side="left", padx=8)
        ttk.Label(controls, text="份任务").pack(side="left")
        ttk.Label(self, text="抽查能发现部分问题；阅读稿生成不代表整本验收通过。", foreground="#555555").grid(
            row=10, column=1, columnspan=2, sticky="w", pady=(0, 10))

        actions = ttk.Frame(self)
        actions.grid(row=11, column=0, columnspan=3, sticky="ew", pady=8)
        self.start_button = ttk.Button(actions, text="开始 / 继续翻译", command=self.start)
        self.start_button.pack(side="left")
        self.draft_button = ttk.Button(actions, text="只生成阅读稿", command=lambda: self.start(draft_only=True))
        self.draft_button.pack(side="left", padx=10)
        self.stop_button = ttk.Button(actions, text="安全停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(actions, text="打开任务目录", command=self.open_output).pack(side="right")
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        ttk.Label(self, textvariable=self.status, font=("Microsoft YaHei UI", 11, "bold")).grid(
            row=13, column=0, columnspan=3, sticky="w")
        ttk.Label(self, textvariable=self.detail, wraplength=820).grid(row=14, column=0, columnspan=3, sticky="w", pady=(5, 10))
        logframe = ttk.Frame(self)
        logframe.grid(row=15, column=0, columnspan=3, sticky="nsew")
        self.rowconfigure(15, weight=1)
        self.log = tk.Text(logframe, height=8, state="disabled", wrap="word", relief="flat", padx=10, pady=8)
        scroll = ttk.Scrollbar(logframe, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def load_config(self, *, show_error=True):
        try:
            choices = read_provider_choices(self.vars["config"].get())
        except ValueError as exc:
            self.choices = ProviderChoices((), (), None)
            self.provider_combo.configure(values=())
            self.review_provider_combo.configure(values=())
            self.vars["provider"].set("")
            self.vars["review_provider"].set("")
            self.status.set("请选择已填写的模型配置文件。")
            if show_error:
                messagebox.showerror("配置未就绪", str(exc), parent=self.master)
            return False
        previous = {"provider": self.choices.name_for(self.vars["provider"].get()),
                    "review_provider": self.choices.review_name_for(self.vars["review_provider"].get())}
        self.choices = choices
        for name in ("provider", "review_provider"):
            labels = choices.review_labels if name == "review_provider" else choices.labels
            names = choices.review_names if name == "review_provider" else choices.names
            getattr(self, name + "_combo").configure(values=labels)
            fallback = choices.default if choices.default in names else next(iter(names), None)
            chosen = previous[name] if previous[name] in names else fallback
            # Prefer a dedicated review role if the configuration already contains it.
            if name == "review_provider" and previous[name] not in names:
                peer = choices.review_default or next((n for n in names if n.endswith("-review")), None)
                chosen = peer or chosen
            self.vars[name].set(choices.label_for(chosen))
        return True

    def choose_pdf(self):
        value = filedialog.askopenfilename(parent=self.master, title="选择原 PDF", filetypes=[("PDF 文件", "*.pdf")])
        if value:
            self.vars["pdf"].set(value)
            if not self.vars["out"].get().strip():
                path = Path(value)
                self.vars["out"].set(str(path.parent / (path.stem + "_翻译任务")))

    def choose_job(self):
        value = filedialog.askdirectory(parent=self.master, title="选择保存目录或已有任务目录", mustexist=False)
        if value:
            self.vars["out"].set(value)
            if (Path(value) / "book.json").is_file():
                self.vars["pdf"].set("")
                self.vars["pages"].set("")
                self.status.set("已选择已有任务；开始后会检查并继续保存的进度。")

    def choose_config(self):
        value = filedialog.askopenfilename(parent=self.master, title="选择模型配置", filetypes=[("JSON 配置", "*.json")])
        if value:
            self.vars["config"].set(value)
            self.load_config()

    def _set_busy(self, busy):
        self._busy = busy
        for widget, state in self._settings:
            widget.configure(state="disabled" if busy else state)
        self.start_button.configure(state="disabled" if busy else "normal")
        self.draft_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def start(self, *, draft_only=False):
        if self._busy:
            return
        # Re-read names so an edited configuration cannot silently keep a stale display label.
        if not draft_only and not self.load_config():
            return
        try:
            options = validate_inputs({key: value.get() for key, value in self.vars.items()},
                                      self.choices, draft_only=draft_only)
        except ValueError as exc:
            messagebox.showerror("请检查填写内容", str(exc), parent=self.master)
            return
        self._set_busy(True)
        self.progress["value"] = 0
        self.status.set("正在准备阅读稿…" if draft_only else "正在检查任务并准备翻译…")
        self.detail.set("进度会自动保存。安全停止会等当前批次保存后生效。")
        self._append_log("只生成阅读稿" if draft_only else "开始处理；继续时会复用匹配的已存译文。")
        self.worker.start(options)

    def stop(self):
        if self._busy:
            self.worker.stop()
            self.stop_button.configure(state="disabled")
            self.status.set("正在安全停止：本批保存后结束…")
            self._append_log("已请求停止，请等待当前批次保存；无需重复点击。")

    def _append_log(self, value):
        value = safe_message(value, self.choices.secrets)
        if value == self._last_log:
            return
        self._last_log = value
        self.log.configure(state="normal")
        self.log.insert("end", value + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "30.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _progress_event(self, event):
        stage = event.get("stage") or event.get("phase")
        message = event.get("message") or STAGE_LABELS.get(stage, "处理中")
        if not self.worker.cancel_event.is_set():
            self.status.set(safe_message(message, self.choices.secrets))
        progress = event.get("progress")
        if isinstance(progress, (float, int)) and not isinstance(progress, bool):
            self.progress["value"] = min(100, max(0, float(progress) * 100))
        summary = event.get("summary") if isinstance(event.get("summary"), dict) else {}
        current, total = event.get("current", summary.get("accepted")), event.get("total", summary.get("total"))
        pieces = []
        if type(current) is int and type(total) is int and total > 0:
            pieces.append(f"已保存 {current:,} / {total:,} 段")
        speed = event.get("speed_elements_per_minute")
        if isinstance(speed, (float, int)) and not isinstance(speed, bool) and speed > 0:
            pieces.append(f"约 {speed:.1f} 段/分钟")
        if pieces:
            self.detail.set("  ·  ".join(pieces) + "。阅读稿仍需检查。")
        self._append_log(message)

    def _poll(self):
        try:
            while True:
                kind, value = self.worker.events.get_nowait()
                if kind == "progress":
                    self._progress_event(value)
                elif kind == "done":
                    state = value.get("state") or value.get("status") or "incomplete"
                    self._set_busy(False)
                    self.status.set(STATE_LABELS.get(state, "本轮已保存，请查看任务目录。"))
                    self._append_log(self.status.get())
                    summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
                    output = (value.get("pdf_path") or value.get("output_path")
                              or value.get("output_pdf") or summary.get("output_pdf"))
                    if output:
                        self._append_log("阅读稿：" + str(output))
                    detail = value.get("message")
                    if isinstance(detail, str) and detail:
                        detail = safe_message(detail, self.choices.secrets)
                        self._append_log(detail)
                        self.detail.set(detail + " 生成成功不代表整本验收通过。")
                    else:
                        self.detail.set("可打开任务目录查看阅读稿和待处理报告；生成成功不代表整本验收通过。")
                elif kind == "error":
                    self._set_busy(False)
                    error_type, error_text = value
                    self.status.set("本轮未完成，已有进度保留。")
                    self._append_log(f"{error_type}：{error_text}")
                    self.detail.set("请检查配置、连接或任务目录，再继续运行。")
        except queue.Empty:
            pass
        if self._closing and not self._busy:
            self.master.destroy()
            return
        self._after_id = self.master.after(120, self._poll)

    def open_output(self):
        try:
            open_directory(self.vars["out"].get())
        except (OSError, ValueError) as exc:
            messagebox.showinfo("打开任务目录", safe_message(exc, self.choices.secrets), parent=self.master)

    def close(self):
        if self._busy:
            self._closing = True
            self.stop()
            self.detail.set("当前批次保存后窗口会自动关闭，请稍候。")
        else:
            if self._after_id:
                self.master.after_cancel(self._after_id)
            self.master.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="教材 PDF 翻译桌面入口")
    parser.add_argument("--job", help="继续已有任务目录")
    parser.add_argument("--config", default=str(APP_ROOT / "config.json"), help="模型配置文件")
    args = parser.parse_args(argv)
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    DesktopApp(root, job=args.job, config_path=args.config)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
