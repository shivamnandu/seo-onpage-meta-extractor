#!/usr/bin/env python3
"""
seo_tool.py - On-Page SEO Extractor (WAF-aware)
================================================
Run with no arguments for the dialog:

    python seo_tool.py

The dialog asks how to supply the URLs (paste them / a sitemap URL / a .txt or .csv file),
lets you set the request rate and WAF-proof mode, shows live progress, and writes into the
output folder:

    seo_extracted_data.csv             the pure extracted values (title, description, ...)
    seo_audit.csv / seo_audit.json     raw tags + parsable/unparsable verdicts (see README.md)
    seo_report_<timestamp>.xlsx        Summary / Extracted Data / Audit / Raw HTML / URLs / Issues
    crawl_summary.json                 totals + proof that --rps was never exceeded
    requests.log errors.log waf_blocks.log parse_warnings.log
    journal.jsonl                      per-URL results, source of truth for "Resume"

With arguments it is the command-line tool (all flags: python seo_tool.py --help), e.g.

    python seo_tool.py --sitemap https://example.com/sitemap.xml --output-dir out
    python seo_tool.py --urls-file urls.txt --waf-mode --rps 1 --concurrency 1
    python seo_tool.py --urls-file urls.txt --output-dir out --resume

The extraction, validation, fetching and rate limiting live in the seo_extractor/ package next
to this file.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from seo_extractor.main import (  # noqa: E402
    build_parser,
    describe_outcome,
    execute,
    main as cli_main,
    run_async,
)
from seo_extractor.url_source import InputError  # noqa: E402
from seo_extractor.utils import setup_logging, shutdown_logging  # noqa: E402

DEFAULT_OUT = str(HERE / "seo_audit_output")


def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    class App(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title("On-Page SEO Extractor  (WAF-aware)")
            self.geometry("800x780")
            self.minsize(720, 660)

            self.mode = tk.StringVar(value="manual")
            self.sitemap_url = tk.StringVar()
            self.file_path = tk.StringVar()
            self.out_dir = tk.StringVar(value=DEFAULT_OUT)
            self.rps = tk.IntVar(value=2)
            self.concurrency = tk.IntVar(value=5)
            self.timeout = tk.DoubleVar(value=15)
            self.max_redirects = tk.IntVar(value=5)
            self.waf_mode = tk.BooleanVar(value=False)
            self.sensitive = tk.StringVar()
            self.resume = tk.BooleanVar(value=False)
            self.excel = tk.BooleanVar(value=True)
            self.dry_run = tk.BooleanVar(value=False)

            self.stop_event = threading.Event()
            self.msgs: queue.Queue = queue.Queue()
            self.worker: threading.Thread | None = None
            self.last_out: str | None = None

            self._build()
            self.after(120, self._drain)

        # ---- layout ---------------------------------------------------------- #
        def _build(self) -> None:
            pad = {"padx": 8, "pady": 4}

            src = ttk.LabelFrame(self, text="1. Where do the URLs come from?")
            src.pack(fill="x", **pad)
            for row, (text, value) in enumerate((
                ("Enter / paste URLs manually", "manual"),
                ("Sitemap URL (sitemap.xml / index / .xml.gz)", "sitemap"),
                ("Select a file that contains the URLs (.txt / .csv)", "file"),
            )):
                ttk.Radiobutton(src, text=text, value=value, variable=self.mode,
                                command=self._sync_mode).grid(row=row, column=0, sticky="w", padx=6, pady=2)

            self.text_box = scrolledtext.ScrolledText(self, height=7, wrap="word")
            self.text_box.pack(fill="both", expand=False, **pad)
            self.text_box.insert("1.0", "https://example.com/\n")

            self.sitemap_row = ttk.Frame(self)
            self.sitemap_row.pack(fill="x", **pad)
            ttk.Label(self.sitemap_row, text="Sitemap URL:").pack(side="left")
            ttk.Entry(self.sitemap_row, textvariable=self.sitemap_url).pack(side="left", fill="x", expand=True, padx=6)

            self.file_row = ttk.Frame(self)
            self.file_row.pack(fill="x", **pad)
            ttk.Label(self.file_row, text="URL file:").pack(side="left")
            ttk.Entry(self.file_row, textvariable=self.file_path).pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(self.file_row, text="Browse...", command=self._pick_file).pack(side="left")

            opt = ttk.LabelFrame(self, text="2. Fetch settings")
            opt.pack(fill="x", **pad)
            self._spin(opt, "Requests / second", self.rps, 0, 0, 1, 50)
            self._spin(opt, "Concurrent requests", self.concurrency, 0, 2, 1, 50)
            self._spin(opt, "Timeout (s)", self.timeout, 1, 0, 1, 120)
            self._spin(opt, "Max redirects", self.max_redirects, 1, 2, 0, 20)
            ttk.Checkbutton(
                opt, text="WAF-proof mode for ALL URLs (Chrome TLS impersonation, 2-5 s gap per domain)",
                variable=self.waf_mode).grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=2)
            ttk.Label(opt, text="WAF-proof only for these domains:").grid(row=3, column=0, columnspan=2, sticky="e", padx=6)
            ttk.Entry(opt, textvariable=self.sensitive).grid(row=3, column=2, columnspan=2, sticky="we", padx=6, pady=2)
            ttk.Label(opt, text="e.g.  *.bank.com, portal.agency.gov   (leave empty for none)",
                      foreground="#666").grid(row=4, column=0, columnspan=4, sticky="w", padx=6)
            opt.columnconfigure(3, weight=1)

            extra = ttk.Frame(self)
            extra.pack(fill="x", **pad)
            ttk.Checkbutton(extra, text="Resume previous run in this folder", variable=self.resume).pack(side="left", padx=6)
            ttk.Checkbutton(extra, text="Write Excel report", variable=self.excel).pack(side="left", padx=6)
            ttk.Checkbutton(extra, text="Dry run (only count URLs)", variable=self.dry_run).pack(side="left", padx=6)

            out = ttk.Frame(self)
            out.pack(fill="x", **pad)
            ttk.Label(out, text="Output folder:").pack(side="left")
            ttk.Entry(out, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(out, text="Browse...", command=self._pick_out).pack(side="left")

            ctl = ttk.Frame(self)
            ctl.pack(fill="x", **pad)
            self.start_btn = ttk.Button(ctl, text="Start", command=self._start)
            self.start_btn.pack(side="left")
            self.stop_btn = ttk.Button(ctl, text="Stop", command=self._stop, state="disabled")
            self.stop_btn.pack(side="left", padx=6)
            self.open_btn = ttk.Button(ctl, text="Open output folder", command=self._open_out)
            self.open_btn.pack(side="left")
            self.progress = ttk.Progressbar(ctl, mode="determinate")
            self.progress.pack(side="left", fill="x", expand=True, padx=6)

            self.log = scrolledtext.ScrolledText(self, height=12, wrap="none", state="disabled")
            self.log.pack(fill="both", expand=True, **pad)
            self._sync_mode()

        @staticmethod
        def _spin(parent, label, var, r, c, lo, hi) -> None:
            ttk.Label(parent, text=label).grid(row=r, column=c, sticky="e", padx=6, pady=2)
            ttk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=8).grid(row=r, column=c + 1, sticky="w", pady=2)

        # ---- behaviour ------------------------------------------------------- #
        def _sync_mode(self) -> None:
            m = self.mode.get()
            self.text_box.configure(state="normal" if m == "manual" else "disabled")
            for w in self.sitemap_row.winfo_children():
                if isinstance(w, ttk.Entry):
                    w.configure(state="normal" if m == "sitemap" else "disabled")
            for w in self.file_row.winfo_children():
                if isinstance(w, (ttk.Entry, ttk.Button)):
                    w.configure(state="normal" if m == "file" else "disabled")

        def _pick_file(self) -> None:
            p = filedialog.askopenfilename(title="Select a file containing URLs",
                                           filetypes=[("Text / CSV", "*.txt *.csv *.list"), ("All files", "*.*")])
            if p:
                self.file_path.set(p)

        def _pick_out(self) -> None:
            p = filedialog.askdirectory(title="Select output folder")
            if p:
                self.out_dir.set(p)

        def _open_out(self) -> None:
            target = self.last_out or self.out_dir.get().strip()
            if target and os.path.isdir(target):
                try:
                    os.startfile(target)  # type: ignore[attr-defined]  # Windows
                except AttributeError:
                    import subprocess
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])
            else:
                messagebox.showinfo("Output folder", "The output folder does not exist yet.")

        def _log(self, line: str) -> None:
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        # ---- run ------------------------------------------------------------- #
        def _build_argv(self, out: Path) -> list[str]:
            """Translate the dialog into the exact CLI flags, so GUI and CLI share one code path."""
            m = self.mode.get()
            if m == "manual":
                text = self.text_box.get("1.0", "end").strip()
                if not text:
                    raise ValueError("Paste at least one URL.")
                out.mkdir(parents=True, exist_ok=True)
                pasted = out / "pasted_urls.txt"
                pasted.write_text(text + "\n", encoding="utf-8")
                argv = ["--urls-file", str(pasted)]
            elif m == "file":
                p = self.file_path.get().strip()
                if not p or not os.path.isfile(p):
                    raise ValueError("Please select a valid URL file.")
                argv = ["--urls-file", p]
            else:
                sm = self.sitemap_url.get().strip()
                if not sm:
                    raise ValueError("Please enter a sitemap URL.")
                argv = ["--sitemap", sm]
            argv += ["--output-dir", str(out), "--rps", str(self.rps.get()),
                     "--concurrency", str(self.concurrency.get()), "--timeout", str(self.timeout.get()),
                     "--max-redirects", str(self.max_redirects.get())]
            if self.waf_mode.get():
                argv.append("--waf-mode")
            if self.sensitive.get().strip():
                argv += ["--sensitive-domains", self.sensitive.get().strip()]
            for flag, var in (("--resume", self.resume), ("--dry-run", self.dry_run)):
                if var.get():
                    argv.append(flag)
            if not self.excel.get():
                argv.append("--no-excel")
            return argv

        def _start(self) -> None:
            if self.worker and self.worker.is_alive():
                return
            out = Path(self.out_dir.get().strip() or DEFAULT_OUT)
            try:
                args = build_parser().parse_args(self._build_argv(out))
            except (ValueError, tk.TclError) as exc:
                messagebox.showerror("Input error", str(exc))
                return
            except SystemExit:  # argparse rejected a value
                messagebox.showerror("Input error", "One of the settings is not valid.")
                return

            self.stop_event.clear()
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.progress.configure(maximum=1, value=0)
            self.last_out = str(out)
            mode = "WAF-proof (all URLs)" if args.waf_mode else (
                f"WAF-proof for {args.sensitive_domains}" if args.sensitive_domains else "standard")
            self._log(f"\n=== Starting | {args.rps} req/s | {args.concurrency} concurrent | {mode} ===")
            self.worker = threading.Thread(target=self._work, args=(args, out), daemon=True)
            self.worker.start()

        def _work(self, args, out: Path) -> None:
            out.mkdir(parents=True, exist_ok=True)
            setup_logging(out, append=args.resume)
            try:
                outcome = run_async(execute(
                    args, out,
                    on_status=lambda s: self.msgs.put(("log", s)),
                    on_progress=lambda d, t, p: self.msgs.put(("progress", (d, t, p))),
                    stop=self.stop_event,
                ))
                self.msgs.put(("done", outcome))
            except InputError as exc:
                self.msgs.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001 - show any failure instead of dying silently
                self.msgs.put(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                shutdown_logging()

        def _stop(self) -> None:
            self.stop_event.set()
            self.stop_btn.configure(state="disabled")
            self._log("Stop requested - finishing in-flight requests, then saving partial results...")

        def _drain(self) -> None:
            try:
                while True:
                    kind, payload = self.msgs.get_nowait()
                    if kind == "log":
                        self._log(payload)
                    elif kind == "progress":
                        done, total, page = payload
                        self.progress.configure(maximum=total, value=done)
                        self._log(self._fmt(done, total, page))
                    elif kind == "done":
                        self._finish(payload)
                    elif kind == "error":
                        self._reset()
                        self._log(f"ERROR: {payload}")
                        messagebox.showerror("Run failed", payload)
            except queue.Empty:
                pass
            self.after(120, self._drain)

        @staticmethod
        def _fmt(done: int, total: int, page) -> str:
            if page.waf_blocked:
                return f"[{done}/{total}] BLOCKED  {page.url}  -> {page.fetch_error}"
            if page.http_status is None:
                return f"[{done}/{total}] ERROR    {page.url}  -> {page.fetch_error}"
            bad = sum(1 for p in page.params if p.status == "unparsable")
            note = f"  ({bad} unparsable)" if bad else ""
            return f"[{done}/{total}] {page.http_status}  {page.url}{note}"

        def _reset(self) -> None:
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")

        def _finish(self, outcome) -> None:
            self._reset()
            if outcome.dry_run:
                messagebox.showinfo("Dry run", f"{len(outcome.urlset.urls)} URL(s) would be crawled.\n"
                                               f"Invalid: {outcome.urlset.invalid_count}   "
                                               f"Duplicates: {outcome.urlset.duplicate_count}")
                return
            text = describe_outcome(outcome)
            self._log("\n" + text)
            s = outcome.summary
            self._open_out()
            messagebox.showinfo(
                "Finished" if not outcome.interrupted else "Stopped",
                f"Crawled {s['total_urls']} URL(s): {s['successful']} ok, {s['failed']} failed, "
                f"{s['waf_blocked']} WAF-blocked.\n"
                f"Elements: {s['parameters']['parsable']} parsable, {s['parameters']['unparsable']} unparsable.\n\n"
                f"Saved to:\n{outcome.out_dir}")

    App().mainloop()


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv == ["--gui"]:
        run_gui()
        return 0
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
