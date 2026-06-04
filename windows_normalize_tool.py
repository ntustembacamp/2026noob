import json
import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from windows_batch_service import (
    BatchNormalizePayload,
    list_tabular_sheets,
    load_excel_columns,
    normalize_headshot_batch,
)
from log_paths import NOOB_LOG_ROOT


APP_TITLE = "AI人臉辨識系統 人員圖檔名稱正規化工具"
DEFAULT_DEST_DIR = r"C:\feature_src"
FEATURE_BUILD_URL = "http://localhost:8000/admin-batch-ui"
TOOL_LOG_DIR = NOOB_LOG_ROOT
TOOL_LOG_PATH = TOOL_LOG_DIR / "windows_normalize_tool.log"


class NormalizeToolApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1280x860")
        self.root.minsize(1120, 760)

        self.source_dir_var = tk.StringVar()
        self.dest_dir_var = tk.StringVar(value=DEFAULT_DEST_DIR)
        self.excel_path_var = tk.StringVar()
        self.sheet_name_var = tk.StringVar()
        self.original_column_var = tk.StringVar()
        self.delimiter_var = tk.StringVar(value="_")
        self.extension_override_var = tk.StringVar()
        self.status_var = tk.StringVar(value="請先選擇 Excel、來源圖檔資料夾與目的圖檔資料夾。")
        self.admin_state_var = tk.StringVar()

        self.run_button: ttk.Button | None = None
        self.clear_button: ttk.Button | None = None
        self.open_output_button: ttk.Button | None = None
        self.open_archive_button: ttk.Button | None = None
        self.open_log_button: ttk.Button | None = None
        self.last_output_dir = ""
        self.last_archive_dir = ""
        self.log_file_path = TOOL_LOG_PATH

        self.sheet_names: list[str] = []
        self.column_vars: dict[str, tk.BooleanVar] = {}

        self._build_ui()
        self._refresh_admin_state()

    def _build_ui(self):
        self.root.configure(bg="#f4efe7")

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text=APP_TITLE, font=("Microsoft JhengHei UI", 20, "bold"))
        title.grid(row=0, column=0, sticky="w")

        subtitle = ttk.Label(
            header,
            text="這是 Windows 本機工具版：專門處理人員圖檔名稱正規化；建立人員base資料及特徵資料請回網頁執行。",
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(4, 10))

        top_actions = ttk.Frame(header)
        top_actions.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        top_actions.columnconfigure(0, weight=1)

        self.admin_label = ttk.Label(top_actions, textvariable=self.admin_state_var)
        self.admin_label.grid(row=0, column=0, sticky="w")

        ttk.Button(
            top_actions,
            text="開啟人員資料及特徵建置頁",
            command=lambda: webbrowser.open(FEATURE_BUILD_URL),
        ).grid(row=0, column=1, sticky="e")
        ttk.Button(
            top_actions,
            text="以系統管理員重新啟動",
            command=self.restart_as_admin,
        ).grid(row=0, column=2, sticky="e", padx=(8, 0))

        top_section = ttk.Frame(outer)
        top_section.grid(row=1, column=0, sticky="ew", pady=(8, 12))
        top_section.columnconfigure(0, weight=1)

        form = ttk.LabelFrame(top_section, text="人員圖檔名稱正規化批次作業", padding=16)
        form.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)

        settings_grid = ttk.Frame(form)
        settings_grid.grid(row=0, column=0, sticky="ew")
        settings_grid.columnconfigure(1, weight=1)
        settings_grid.columnconfigure(4, weight=1)

        self._inline_path_row(
            settings_grid,
            row=0,
            label_text="來源圖檔資料夾",
            variable=self.source_dir_var,
            button_command=self.pick_source_dir,
        )
        self._inline_path_row(
            settings_grid,
            row=0,
            label_text="目的圖檔資料夾",
            variable=self.dest_dir_var,
            button_command=self.pick_dest_dir,
            col_offset=3,
        )
        self._inline_path_row(
            settings_grid,
            row=1,
            label_text="Excel 檔案",
            variable=self.excel_path_var,
            button_command=self.pick_excel_file,
            file_mode=True,
        )

        ttk.Label(settings_grid, text="工作表").grid(row=1, column=3, sticky="w", pady=8, padx=(20, 8))
        sheet_box = ttk.Frame(settings_grid)
        sheet_box.grid(row=1, column=4, sticky="ew", pady=8)
        sheet_box.columnconfigure(0, weight=1)
        self.sheet_combo = ttk.Combobox(sheet_box, textvariable=self.sheet_name_var, state="readonly")
        self.sheet_combo.grid(row=0, column=0, sticky="ew")
        self.sheet_combo.bind("<<ComboboxSelected>>", lambda _event: self.load_columns())
        ttk.Button(sheet_box, text="讀取欄位", command=self.load_columns).grid(row=0, column=1, padx=(10, 0))

        ttk.Label(settings_grid, text="請選擇原始檔名欄位").grid(row=2, column=0, sticky="w", pady=8, padx=(0, 8))
        self.original_column_combo = ttk.Combobox(settings_grid, textvariable=self.original_column_var, state="readonly")
        self.original_column_combo.grid(row=2, column=1, sticky="ew", pady=8)

        ttk.Label(settings_grid, text="檔名組合字元").grid(row=2, column=3, sticky="w", pady=8, padx=(20, 8))
        ttk.Entry(settings_grid, textvariable=self.delimiter_var, width=12).grid(row=2, column=4, sticky="w", pady=8)

        ttk.Label(settings_grid, text="副檔名覆寫").grid(row=3, column=0, sticky="w", pady=8, padx=(0, 8))
        ttk.Entry(settings_grid, textvariable=self.extension_override_var, width=20).grid(row=3, column=1, sticky="w", pady=8)

        column_box = ttk.LabelFrame(top_section, text="檔名組合欄位", padding=16)
        column_box.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.columns_frame = ttk.Frame(column_box)
        self.columns_frame.pack(fill="x")
        self.columns_hint = ttk.Label(
            column_box,
            text="從 Excel 欄位中勾選要組成新檔名的欄位，例如：系所、級別、小隊、姓名。",
        )
        self.columns_hint.pack(anchor="w", pady=(10, 0))

        run_bar = ttk.Frame(top_section)
        run_bar.grid(row=2, column=0, sticky="ew")

        self.run_button = ttk.Button(run_bar, text="執行人員圖檔名稱正規化", command=self.run_normalize)
        self.run_button.pack(side="left")
        self.clear_button = ttk.Button(run_bar, text="清空結果", command=self.clear_result)
        self.clear_button.pack(side="left", padx=(8, 0))
        self.open_output_button = ttk.Button(
            run_bar,
            text="開啟輸出資料夾",
            command=lambda: self.open_folder(self.last_output_dir),
            state="disabled",
        )
        self.open_output_button.pack(side="left", padx=(16, 0))
        self.open_archive_button = ttk.Button(
            run_bar,
            text="開啟備份資料夾",
            command=lambda: self.open_folder(self.last_archive_dir),
            state="disabled",
        )
        self.open_archive_button.pack(side="left", padx=(8, 0))
        self.open_log_button = ttk.Button(run_bar, text="開啟執行狀態 Log", command=self.open_log_file)
        self.open_log_button.pack(side="left", padx=(16, 0))

        status_frame = ttk.LabelFrame(outer, text="執行狀態", padding=16)
        status_frame.grid(row=3, column=0, sticky="nsew")
        status_frame.columnconfigure(0, weight=1)
        status_frame.rowconfigure(1, weight=1)

        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, foreground="#92400e")
        self.status_label.grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.result_text = ScrolledText(status_frame, height=14, wrap="word", font=("Consolas", 10))
        self.result_text.grid(row=1, column=0, sticky="nsew")

    def _inline_path_row(self, parent, row, label_text, variable, button_command, col_offset=0, file_mode=False):
        ttk.Label(parent, text=label_text).grid(row=row, column=col_offset, sticky="w", pady=8, padx=(0, 8))
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=col_offset + 1, sticky="ew", pady=8)
        button_label = "選擇檔案" if file_mode else "選擇資料夾"
        ttk.Button(parent, text=button_label, command=button_command).grid(
            row=row,
            column=col_offset + 2,
            padx=(10, 0),
            pady=8,
        )

    def _refresh_admin_state(self):
        try:
            import ctypes

            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False

        if is_admin:
            self.admin_state_var.set("目前狀態：系統管理員模式")
            self.admin_label.configure(foreground="#0f766e")
        else:
            self.admin_state_var.set("目前狀態：一般模式；若要寫入 C:\\feature_src，建議改用系統管理員模式。")
            self.admin_label.configure(foreground="#b45309")

    def restart_as_admin(self):
        try:
            import ctypes

            params = " ".join([f'"{arg}"' for arg in sys.argv])
            result = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                sys.executable,
                params,
                str(Path(__file__).resolve().parent),
                1,
            )
            if result <= 32:
                raise RuntimeError("無法切換為系統管理員模式")
            self.root.after(300, self.root.destroy)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"以管理員重啟失敗：{exc}")

    def pick_source_dir(self):
        selected = filedialog.askdirectory(title="選擇來源圖檔資料夾")
        if selected:
            self.source_dir_var.set(selected)

    def pick_dest_dir(self):
        selected = filedialog.askdirectory(title="選擇目的圖檔資料夾", initialdir=self.dest_dir_var.get() or None)
        if selected:
            self.dest_dir_var.set(selected)

    def pick_excel_file(self):
        selected = filedialog.askopenfilename(
            title="選擇 Excel 或 CSV",
            filetypes=[
                ("Excel / CSV", "*.xlsx *.xls *.xlsm *.csv"),
                ("Excel", "*.xlsx *.xls *.xlsm"),
                ("CSV", "*.csv"),
                ("All files", "*.*"),
            ],
        )
        if not selected:
            return

        self.excel_path_var.set(selected)
        self.load_sheets()

    def load_sheets(self):
        excel_path = self.excel_path_var.get().strip()
        if not excel_path:
            messagebox.showwarning(APP_TITLE, "請先選擇 Excel 或 CSV 檔案。")
            return

        try:
            self.sheet_names = list_tabular_sheets(excel_path)
            self.sheet_combo["values"] = self.sheet_names
            if self.sheet_names:
                self.sheet_name_var.set(self.sheet_names[0])
            self.load_columns()
            self.status_var.set(f"已讀取工作表：{', '.join(self.sheet_names)}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"讀取工作表失敗：{exc}")

    def load_columns(self):
        excel_path = self.excel_path_var.get().strip()
        if not excel_path:
            return

        try:
            sheet_name = self.sheet_name_var.get().strip()
            columns = load_excel_columns(excel_path, sheet_name=sheet_name if sheet_name != "CSV" else "")
            self.original_column_combo["values"] = columns
            if columns:
                self.original_column_var.set(columns[0])
            self.render_column_checkboxes(columns)
            self.status_var.set(f"已讀取 {len(columns)} 個欄位。")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"讀取欄位失敗：{exc}")

    def render_column_checkboxes(self, columns: list[str]):
        for child in self.columns_frame.winfo_children():
            child.destroy()
        self.column_vars.clear()

        columns_per_row = 6
        for col in range(columns_per_row):
            self.columns_frame.columnconfigure(col, weight=1)

        default_checked = {"系所", "級別", "小隊", "姓名"}
        for index, column in enumerate(columns):
            var = tk.BooleanVar(value=column in default_checked)
            self.column_vars[column] = var
            ttk.Checkbutton(self.columns_frame, text=column, variable=var).grid(
                row=index // columns_per_row,
                column=index % columns_per_row,
                sticky="w",
                padx=(0, 12),
                pady=6,
            )

    def selected_filename_fields(self):
        return [name for name, var in self.column_vars.items() if var.get()]

    def clear_result(self):
        self.result_text.delete("1.0", "end")
        self.status_var.set("已清空執行結果。")
        self.last_output_dir = ""
        self.last_archive_dir = ""
        if self.open_output_button is not None:
            self.open_output_button.configure(state="disabled")
        if self.open_archive_button is not None:
            self.open_archive_button.configure(state="disabled")

    def append_log(self, message: str):
        line = message.rstrip()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"[{timestamp}] {line}" if line else ""
        self.result_text.insert("end", formatted + "\n")
        self.result_text.see("end")
        self._append_log_file(formatted)
        self.root.update_idletasks()

    def append_log_async(self, message: str):
        self.root.after(0, lambda: self.append_log(message))

    def open_folder(self, folder_path: str):
        if not folder_path:
            messagebox.showwarning(APP_TITLE, "目前沒有可開啟的資料夾。")
            return
        folder = Path(folder_path)
        if not folder.exists():
            messagebox.showwarning(APP_TITLE, f"找不到資料夾：{folder}")
            return
        os.startfile(str(folder))

    def open_log_file(self):
        try:
            TOOL_LOG_DIR.mkdir(parents=True, exist_ok=True)
            if not self.log_file_path.exists():
                self.log_file_path.write_text("", encoding="utf-8")
            subprocess.Popen(["notepad.exe", str(self.log_file_path)])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"無法開啟 log：{exc}")

    def _reset_log_file(self):
        TOOL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_file_path.write_text("", encoding="utf-8")

    def _append_log_file(self, line: str):
        TOOL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with self.log_file_path.open("a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")

    def run_normalize(self):
        try:
            payload = self.build_payload()
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        self.status_var.set("正規化處理中，請稍候...")
        self.result_text.delete("1.0", "end")
        self._reset_log_file()
        self.append_log("開始執行人員圖檔名稱正規化")
        self.append_log(f"來源圖檔資料夾: {payload.source_dir}")
        self.append_log(f"目的圖檔資料夾: {payload.destination_dir}")
        self.append_log(f"Excel 檔案: {payload.excel_path}")
        self.append_log(f"工作表: {payload.sheet_name or 'CSV'}")
        self.append_log(f"原始檔名欄位: {payload.original_filename_column}")
        self.append_log(f"檔名組合欄位: {', '.join(payload.filename_fields)}")

        if self.run_button is not None:
            self.run_button.configure(state="disabled")
        if self.clear_button is not None:
            self.clear_button.configure(state="disabled")
        if self.open_output_button is not None:
            self.open_output_button.configure(state="disabled")
        if self.open_archive_button is not None:
            self.open_archive_button.configure(state="disabled")
        self.root.update_idletasks()

        worker = threading.Thread(target=self._run_normalize_worker, args=(payload,), daemon=True)
        worker.start()

    def build_payload(self):
        source_dir = self.source_dir_var.get().strip()
        dest_dir = self.dest_dir_var.get().strip()
        excel_path = self.excel_path_var.get().strip()
        sheet_name = self.sheet_name_var.get().strip()
        original_column = self.original_column_var.get().strip()
        filename_fields = self.selected_filename_fields()

        if not source_dir:
            raise ValueError("請先指定來源圖檔資料夾。")
        if not dest_dir:
            raise ValueError("請先指定目的圖檔資料夾。")
        if not excel_path:
            raise ValueError("請先選擇 Excel 或 CSV 檔案。")
        if not original_column:
            raise ValueError("請先選擇原始檔名欄位。")
        if not filename_fields:
            raise ValueError("請至少勾選 1 個檔名組合欄位。")

        normalized_sheet = "" if sheet_name == "CSV" else sheet_name
        return BatchNormalizePayload(
            source_dir=source_dir,
            destination_dir=dest_dir,
            excel_path=excel_path,
            sheet_name=normalized_sheet,
            original_filename_column=original_column,
            filename_fields=filename_fields,
            delimiter=self.delimiter_var.get() or "_",
            extension_override=self.extension_override_var.get().strip(),
        )

    def _run_normalize_worker(self, payload: BatchNormalizePayload):
        try:
            self.append_log_async("讀取 Excel 欄位與資料中...")
            result = normalize_headshot_batch(payload)
            self.append_log_async("開始搬移與封存成功處理檔案...")
            for item in result.get("processed", []):
                self.append_log_async(f"成功: {item.get('original_name')} -> {item.get('normalized_name')}")
            for item in result.get("archived_files", []):
                self.append_log_async(f"封存: {item.get('original_name')} -> {item.get('archive_file')}")
            for item in result.get("missing_files", []):
                self.append_log_async(f"缺檔: {item}")
            for item in result.get("duplicate_targets", []):
                self.append_log_async(f"重複目標檔名: {item}")
            self.root.after(0, lambda: self._show_success(result))
        except Exception as exc:
            self.root.after(0, lambda: self._show_error(exc))

    def _show_success(self, result: dict):
        summary = (
            f"完成：成功 {result.get('processed_count', 0)} 筆，"
            f"缺檔 {result.get('missing_count', 0)} 筆，"
            f"重複目標 {result.get('duplicate_target_count', 0)} 筆\n"
            f"輸出資料夾：{result.get('destination_host_dir') or result.get('destination_dir')}\n"
            f"備份資料夾：{result.get('archive_host_dir') or result.get('archive_dir')}"
        )
        self.last_output_dir = result.get("destination_host_dir") or result.get("destination_dir") or ""
        self.last_archive_dir = result.get("archive_host_dir") or result.get("archive_dir") or ""
        self.status_var.set(summary)
        self.append_log("")
        self.append_log(summary)
        self.append_log("")
        self.append_log(json.dumps(result, ensure_ascii=False, indent=2))
        if self.run_button is not None:
            self.run_button.configure(state="normal")
        if self.clear_button is not None:
            self.clear_button.configure(state="normal")
        if self.open_output_button is not None and self.last_output_dir:
            self.open_output_button.configure(state="normal")
        if self.open_archive_button is not None and self.last_archive_dir:
            self.open_archive_button.configure(state="normal")
        self.root.update_idletasks()
        messagebox.showinfo(APP_TITLE, summary)

    def _show_error(self, exc: Exception):
        self.status_var.set(f"執行失敗：{exc}")
        self.append_log(f"執行失敗：{exc}")
        if self.run_button is not None:
            self.run_button.configure(state="normal")
        if self.clear_button is not None:
            self.clear_button.configure(state="normal")
        self.root.update_idletasks()
        messagebox.showerror(APP_TITLE, f"執行失敗：{exc}")


def main():
    root = tk.Tk()
    try:
        root.iconname(APP_TITLE)
    except Exception:
        pass
    NormalizeToolApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
