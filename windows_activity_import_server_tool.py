import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText


APP_TITLE = "AI人臉辨識系統 - 活動照片匯入工具（Server 版）"
DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_SOURCE = r"C:\activity\ingest\normalized_success"
LOG_PATH = Path(__file__).resolve().parent / "logs" / "windows_activity_import_server_tool.log"


def post_form_json(url: str, data: dict, timeout: int = 30) -> dict:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body)


def get_json(url: str, timeout: int = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1180x820")
        self.root.minsize(1080, 760)

        self.api_base = tk.StringVar(value=DEFAULT_API_BASE)
        self.laptop_number = tk.StringVar(value="SERVER")
        self.photographer = tk.StringVar()
        self.schedule_id = tk.StringVar()
        self.normalize_mode = tk.StringVar(value="schedule")
        self.enable_pyiqa = tk.BooleanVar(value=False)
        self.source_folder = tk.StringVar(value=DEFAULT_SOURCE)
        self.job_id = tk.StringVar()
        self.status = tk.StringVar(value="請設定欄位後，按「啟動匯入任務」。")

        self.log_offset = 0
        self._stop_poll = False
        self._build_ui()

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        form = ttk.LabelFrame(frame, text="任務設定", padding=12)
        form.grid(row=0, column=0, sticky="ew")
        for col in range(4):
            form.columnconfigure(col, weight=1)

        ttk.Label(form, text="API Base").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.api_base).grid(row=0, column=1, sticky="ew", padx=(4, 12), pady=4)
        ttk.Label(form, text="筆電編號").grid(row=0, column=2, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.laptop_number).grid(row=0, column=3, sticky="ew", padx=(4, 0), pady=4)

        ttk.Label(form, text="攝影師").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.photographer).grid(row=1, column=1, sticky="ew", padx=(4, 12), pady=4)
        ttk.Label(form, text="活動行程 ID").grid(row=1, column=2, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.schedule_id).grid(row=1, column=3, sticky="ew", padx=(4, 0), pady=4)

        ttk.Label(form, text="正規化模式").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(
            form,
            textvariable=self.normalize_mode,
            values=["exif", "schedule"],
            state="readonly",
        ).grid(row=2, column=1, sticky="ew", padx=(4, 12), pady=4)
        ttk.Checkbutton(form, text="啟用 pyiqa_score", variable=self.enable_pyiqa).grid(row=2, column=2, columnspan=2, sticky="w", pady=4)

        ttk.Label(form, text="來源資料夾").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.source_folder).grid(row=3, column=1, columnspan=2, sticky="ew", padx=(4, 12), pady=4)
        ttk.Button(form, text="選擇資料夾", command=self.pick_source_folder).grid(row=3, column=3, sticky="ew", pady=4)

        action = ttk.Frame(frame)
        action.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        ttk.Button(action, text="啟動匯入任務", command=self.start_job).pack(side="left")
        ttk.Button(action, text="接續查看 Job", command=self.attach_job).pack(side="left", padx=(8, 0))
        ttk.Label(action, text="Job ID").pack(side="left", padx=(16, 4))
        ttk.Entry(action, textvariable=self.job_id, width=32).pack(side="left")
        ttk.Button(action, text="開啟 API 頁面", command=lambda: webbrowser.open(f"{self.api_base.get().rstrip('/')}/activity-photo-import-ui")).pack(side="right")

        status_box = ttk.LabelFrame(frame, text="執行狀態", padding=12)
        status_box.grid(row=2, column=0, sticky="nsew")
        status_box.columnconfigure(0, weight=1)
        status_box.rowconfigure(1, weight=1)
        ttk.Label(status_box, textvariable=self.status).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.log_text = ScrolledText(status_box, height=26, wrap="word", font=("Consolas", 10))
        self.log_text.grid(row=1, column=0, sticky="nsew")

        log_actions = ttk.Frame(status_box)
        log_actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(log_actions, text="清空畫面", command=lambda: self.log_text.delete("1.0", tk.END)).pack(side="left")
        ttk.Button(log_actions, text="開啟本機工具 Log", command=self.open_local_log).pack(side="left", padx=(8, 0))
        ttk.Button(log_actions, text="複製錯誤摘要", command=self.copy_error_summary).pack(side="left", padx=(8, 0))

    def append_log(self, text: str):
        self.log_text.insert(tk.END, text.rstrip() + "\n")
        self.log_text.see(tk.END)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8-sig", newline="\n") as handle:
            handle.write(text.rstrip() + "\n")

    def pick_source_folder(self):
        selected = filedialog.askdirectory()
        if selected:
            self.source_folder.set(selected)

    def _payload(self) -> dict:
        payload = {
            "laptop_number": self.laptop_number.get().strip() or "SERVER",
            "photographer": self.photographer.get().strip(),
            "enable_pyiqa": "true" if self.enable_pyiqa.get() else "false",
            "normalize_mode": self.normalize_mode.get().strip() or "schedule",
            "source_folder": self.source_folder.get().strip() or DEFAULT_SOURCE,
            "output_folder": "",
            "backup_folder": "",
        }
        sid = self.schedule_id.get().strip()
        if sid:
            payload["schedule_id"] = sid
        return payload

    def start_job(self):
        self._stop_poll = False
        self.log_offset = 0
        self.status.set("啟動匯入任務中...")
        self.append_log("=== 啟動匯入任務 ===")
        threading.Thread(target=self._start_job_worker, daemon=True).start()

    def _start_job_worker(self):
        try:
            api_base = self.api_base.get().strip().rstrip("/")
            payload = self._payload()
            data = post_form_json(f"{api_base}/activity-photo-import/start", payload, timeout=60)
            job_id = str(data.get("job_id") or "").strip()
            if not job_id:
                raise RuntimeError(f"啟動失敗，回傳缺少 job_id：{data}")
            self.job_id.set(job_id)
            self.status.set(f"任務已啟動：{job_id}")
            self.append_log(f"任務已啟動：{job_id}")
            self._poll_loop(job_id)
        except Exception as exc:
            self.status.set(f"啟動失敗：{exc}")
            self.append_log(f"啟動失敗：{exc}")

    def attach_job(self):
        job_id = self.job_id.get().strip()
        if not job_id:
            messagebox.showwarning(APP_TITLE, "請先輸入 job_id。")
            return
        self._stop_poll = False
        self.status.set(f"接續查看任務：{job_id}")
        self.append_log(f"=== 接續查看：{job_id} ===")
        threading.Thread(target=self._poll_loop, args=(job_id,), daemon=True).start()

    def _poll_loop(self, job_id: str):
        api_base = self.api_base.get().strip().rstrip("/")
        for _ in range(7200):
            if self._stop_poll:
                return
            try:
                status = get_json(f"{api_base}/activity-photo-import/jobs/{urllib.parse.quote(job_id)}", timeout=20)
                logs = get_json(f"{api_base}/activity-photo-import/jobs/{urllib.parse.quote(job_id)}/logs?offset={self.log_offset}", timeout=20)
                lines = logs.get("lines") or []
                self.log_offset = int(logs.get("next_offset") or self.log_offset)
                for line in lines:
                    self.append_log(str(line))

                text = (
                    f"job={job_id} 狀態={status.get('status')} "
                    f"總數={status.get('total_count', 0)} 已處理={status.get('processed_count', 0)} "
                    f"成功={status.get('success_count', 0)} 失敗={status.get('failed_count', 0)} "
                    f"略過={status.get('skipped_count', 0)} 重複={status.get('duplicate_count', 0)} "
                    f"來源剩餘={status.get('remaining_in_source_count', 0)}"
                )
                self.status.set(text)

                if str(status.get("status")) in {"DONE", "FAILED", "CANCELED"}:
                    self.append_log(f"=== 任務結束：{status.get('status')} ===")
                    return
                time.sleep(1.0)
            except urllib.error.HTTPError as exc:
                self.status.set(f"查詢任務失敗：HTTP {exc.code}")
                self.append_log(f"查詢任務失敗：HTTP {exc.code}")
                time.sleep(2.0)
            except Exception as exc:
                self.status.set(f"查詢任務失敗：{exc}")
                self.append_log(f"查詢任務失敗：{exc}")
                time.sleep(2.0)
        self.status.set("輪詢逾時：任務可能仍在背景執行，可稍後再接續查看。")
        self.append_log("輪詢逾時：任務可能仍在背景執行，可稍後再接續查看。")

    def open_local_log(self):
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not LOG_PATH.exists():
            LOG_PATH.write_text("", encoding="utf-8-sig", newline="\n")
        try:
            import subprocess
            subprocess.Popen(["notepad.exe", str(LOG_PATH)], shell=False)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"開啟 Log 失敗：{exc}")

    def copy_error_summary(self):
        content = self.log_text.get("1.0", tk.END)
        lines = [line for line in content.splitlines() if ("失敗" in line or "FAILED" in line or "error" in line.lower())]
        summary = "\n".join(lines[-50:]) if lines else "目前沒有失敗摘要。"
        self.root.clipboard_clear()
        self.root.clipboard_append(summary)
        self.status.set("已複製錯誤摘要到剪貼簿。")


def main():
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
