"""Desktop login window (tkinter, included with Python on Windows).

Connect your Kalshi API key: reuse the key from an old kbot install, or
enter the key ID and choose the private-key file.  Everything stays on this
computer.
"""
from __future__ import annotations

import asyncio
import threading

from . import login as L
from .config import load_settings, save_settings
from .kalshi import load_keys, save_keys


def run_window() -> bool:
    import tkinter as tk
    from tkinter import filedialog, ttk

    s = load_settings()
    existing = load_keys()
    kbot_env = L.find_kbot_env()
    kbot_key = L.read_kbot_key(kbot_env) if kbot_env else None
    saved = {"ok": False}

    root = tk.Tk()
    root.title("adaptbot - connect Kalshi")
    root.geometry("560x520")
    root.minsize(480, 460)
    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Connect your Kalshi account", font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(frm, wraplength=520, foreground="#555",
              text="Use the API key you already have (the one kbot used). It stays on this computer.").pack(
        anchor="w", pady=(2, 10))

    status = tk.StringVar(value=f"Currently saved key: {existing[0][:8]}..." if existing else "")
    msg = ttk.Label(frm, textvariable=status, wraplength=520, justify="left")

    env = tk.StringVar(value=s.kalshi_env)
    kid = tk.StringVar()
    keyfile = tk.StringVar()

    def run_bg(fn, done_text=None, close=False):
        status.set("Connecting to Kalshi...")
        for b in buttons:
            b.state(["disabled"])

        def work():
            try:
                text = fn()
                ok = True
            except Exception as e:  # noqa: BLE001
                text, ok = friendly(e), False

            def finish():
                status.set(text + ("\n" + done_text if ok and done_text else ""))
                msg.configure(foreground="#137a3a" if ok else "#b42318")
                for b in buttons:
                    b.state(["!disabled"])
                if ok and close:
                    saved["ok"] = True
                    root.after(1800, root.destroy)
            root.after(0, finish)
        threading.Thread(target=work, daemon=True).start()

    def friendly(e: Exception) -> str:
        t = str(e)
        if "401" in t or "403" in t:
            return ("Kalshi rejected the key: wrong key ID, wrong private key, or a demo key used on "
                    "kalshi.com.\n" + t[:160])
        if "getaddrinfo" in t or "Cannot connect" in t or "DNS" in t:
            return "Could not reach Kalshi - check your internet connection.\n" + t[:160]
        return "Failed: " + t[:300]

    def current_key() -> tuple[str, str]:
        k = kid.get().strip()
        if not k:
            raise ValueError("Enter the API key ID.")
        pem = pem_box.get("1.0", "end").strip()
        path = keyfile.get().strip()
        if pem:
            try:
                L._load_pem(pem)
            except Exception:  # noqa: BLE001
                raise ValueError("The pasted private key could not be read. Choose the original file instead.")
            return k, L.write_key(pem)
        if path:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            try:
                L._load_pem(text)
            except Exception:  # noqa: BLE001
                raise ValueError("That file is not a readable private key (.pem/.key from Kalshi).")
            return k, L.write_key(text)
        if existing and existing[0] == k:
            return existing
        raise ValueError("Choose the private-key file (or paste it).")

    def test(save: bool):
        def fn():
            k, p = current_key()
            text = asyncio.run(L.test_key(env.get(), k, p))
            if save:
                save_keys(k, p)
                s.kalshi_env = env.get()
                save_settings(s)
            return text
        run_bg(fn, "Saved. Next: run Check (2) from the menu." if save else None, close=save)

    def use_kbot():
        def fn():
            text = asyncio.run(L.test_key(env.get(), *kbot_key))
            save_keys(*kbot_key)
            s.kalshi_env = env.get()
            save_settings(s)
            return text
        run_bg(fn, "Saved - using your kbot key.", close=True)

    buttons = []
    if kbot_key:
        box = ttk.LabelFrame(frm, text="Found your kbot install", padding=8)
        box.pack(fill="x", pady=(0, 10))
        b = ttk.Button(box, text=f"Use my kbot key ({kbot_key[0][:8]}...)", command=use_kbot)
        b.pack(anchor="w")
        buttons.append(b)

    row = ttk.Frame(frm)
    row.pack(fill="x")
    ttk.Label(row, text="Account:").pack(side="left")
    ttk.Radiobutton(row, text="kalshi.com (real money)", value="prod", variable=env).pack(side="left", padx=6)
    ttk.Radiobutton(row, text="Demo", value="demo", variable=env).pack(side="left")

    ttk.Label(frm, text="API key ID").pack(anchor="w", pady=(10, 2))
    ttk.Entry(frm, textvariable=kid).pack(fill="x")

    ttk.Label(frm, text="Private key file (.pem / .key downloaded from Kalshi)").pack(anchor="w", pady=(10, 2))
    fr = ttk.Frame(frm)
    fr.pack(fill="x")
    ttk.Entry(fr, textvariable=keyfile).pack(side="left", fill="x", expand=True)

    def browse():
        p = filedialog.askopenfilename(title="Kalshi private key",
                                       filetypes=[("Key files", "*.pem *.key *.txt"), ("All files", "*.*")])
        if p:
            keyfile.set(p)
    b = ttk.Button(fr, text="Browse...", command=browse)
    b.pack(side="left", padx=(6, 0))
    buttons.append(b)

    ttk.Label(frm, text="...or paste the key text").pack(anchor="w", pady=(10, 2))
    pem_box = tk.Text(frm, height=5, font=("Consolas", 9))
    pem_box.pack(fill="x")

    br = ttk.Frame(frm)
    br.pack(fill="x", pady=12)
    for text, cmd in (("Test connection", lambda: test(False)), ("Save", lambda: test(True))):
        b = ttk.Button(br, text=text, command=cmd)
        b.pack(side="left", padx=(0, 8))
        buttons.append(b)
    msg.pack(anchor="w", fill="x")

    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return saved["ok"]
