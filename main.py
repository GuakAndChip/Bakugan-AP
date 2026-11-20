import sys
import traceback

from config import app_dir


def _report_crash() -> None:
    """The exe has no console, so an unexpected error would vanish. Save it and show it instead."""
    text = traceback.format_exc()
    try:
        (app_dir() / "crash.log").write_text(text, encoding="utf-8")
    except OSError:
        pass
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Bakugan AP Client crashed", f"{text[-1500:]}\n\nSaved to {app_dir() / 'crash.log'}")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        from gui import run
        run()
    except Exception:
        _report_crash()
        sys.exit(1)
