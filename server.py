import socket
import threading
import tkinter as tk
from tkinter import scrolledtext
from datetime import datetime

from protocol import (
    TYPE_TEXT, TYPE_LIVE,
    send_message, send_text, FrameReceiver,
    pack_live_payload,
    build_rtsp_response, parse_rtsp_message,
)

CONTROL_PORT = 7777
LIVE_PORT    = 7778
HOST         = '0.0.0.0'

is_stopped     = False
control_socket = None
live_socket    = None

clients      = []           
clients_lock = threading.Lock()

live_streams    = {}         
live_state_lock = threading.Lock()

#GUI
root = tk.Tk()
root.title("Live Stream Server")
root.geometry("420x420")
root.configure(bg="#1e1e2e")

BG     = "#1e1e2e"
FG     = "#e6e6e6"
MUTED  = "#9a9ab0"
ACCENT = "#5fb3ff"
RED    = "#ff6b6b"
BOX_BG = "#161622"

FONT_H1   = ("Segoe UI", 14, "bold")
FONT      = ("Segoe UI", 10)
FONT_MONO = ("Consolas", 9)

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    log_box.insert(tk.END, f"{ts}  {msg}\n")
    log_box.see(tk.END)

def broadcast(msg_type, payload):
    with clients_lock:
        dead = []
        for c in clients:
            try:
                send_message(c["conn"], msg_type, payload, lock=c["lock"])
            except Exception:
                dead.append(c)
        for c in dead:
            clients.remove(c)
    if dead:
        root.after(0, update_counts)

def broadcast_text(text):
    broadcast(TYPE_TEXT, text.encode())

def update_counts():
    with clients_lock:
        n_clients = len(clients)
    with live_state_lock:
        n_live = len(live_streams)
    status_var.set(f"{n_clients} connected   |   {n_live} live")

def end_stream(stream_id, notify=True):
    with live_state_lock:
        entry = live_streams.pop(stream_id, None)
    if entry is None:
        return
    conn = entry.get("conn")
    if conn:
        try: conn.close()
        except Exception: pass
    if notify:
        broadcast_text(f"ENDED:{stream_id}")
    root.after(0, update_counts)
    root.after(0, lambda: log(f"Stream ended: {stream_id}"))

def relay_upload(conn):
    receiver  = FrameReceiver()
    stream_id = None
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            receiver.feed(chunk)
            for msg_type, payload in receiver.pop_messages():
                if stream_id is None:
                    if msg_type != TYPE_TEXT:
                        continue
                    text = payload.decode(errors="ignore").strip()
                    if not text.startswith("ID:"):
                        continue
                    candidate = text.split("ID:", 1)[1].strip()
                    with live_state_lock:
                        entry = live_streams.get(candidate)
                        if entry is not None and entry["conn"] is None:
                            entry["conn"] = conn
                            stream_id = candidate
                    if stream_id is None:
                        return
                    continue
                if msg_type == TYPE_LIVE:
                    broadcast(TYPE_LIVE, pack_live_payload(stream_id, payload))
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass
        if stream_id is not None:
            end_stream(stream_id, notify=True)

def accept_uploads():
    while not is_stopped:
        try:
            live_socket.settimeout(1.0)
            conn, addr = live_socket.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        threading.Thread(target=relay_upload, args=(conn,), daemon=True).start()

def listen_to_client(conn, addr):
    label = f"{addr[0]}:{addr[1]}"
    info = {"conn": conn, "addr": addr, "lock": threading.Lock(), "label": label}
    with clients_lock:
        clients.append(info)
    root.after(0, update_counts)
    root.after(0, lambda: log(f"Connected: {label}"))

    with live_state_lock:
        active = list(live_streams.keys())
    for sid in active:
        try:
            send_text(conn, f"STARTED:{sid}", lock=info["lock"])
        except Exception:
            pass

    receiver = FrameReceiver()
    while not is_stopped:
        try:
            chunk = conn.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            receiver.feed(chunk)
            for msg_type, payload in receiver.pop_messages():
                if msg_type != TYPE_TEXT:
                    continue
                cmd = payload.decode(errors="ignore").strip()
                if not cmd:
                    continue
                method, headers = parse_rtsp_message(cmd)
                cseq = headers.get("CSeq")

                if method == "SETUP":
                    with live_state_lock:
                        live_streams[label] = {"ip": addr[0], "conn": None, "state": "SETUP"}
                    send_text(conn, build_rtsp_response(cseq, session=label), lock=info["lock"])
                    root.after(0, lambda: log(f"SETUP from {label} (Session: {label})"))

                elif method == "PLAY":
                    with live_state_lock:
                        entry = live_streams.get(label)
                        if entry is not None:
                            entry["state"] = "PLAY"
                    if entry is None:
                        send_text(conn, build_rtsp_response(cseq, code=454, reason="Session Not Found"),
                                   lock=info["lock"])
                        continue
                    send_text(conn, build_rtsp_response(cseq, session=label), lock=info["lock"])
                    broadcast_text(f"STARTED:{label}")
                    root.after(0, update_counts)
                    root.after(0, lambda: log(f"PLAY from {label} — now live"))

                elif method == "TEARDOWN":
                    with live_state_lock:
                        was_live = label in live_streams
                    send_text(conn, build_rtsp_response(cseq, session=label), lock=info["lock"])
                    if was_live:
                        end_stream(label, notify=True)
        except Exception:
            break

    with clients_lock:
        clients[:] = [c for c in clients if c["conn"] is not conn]
    root.after(0, update_counts)

    with live_state_lock:
        was_live = label in live_streams
    if was_live:
        end_stream(label, notify=True)

    root.after(0, lambda: log(f"Disconnected: {label}"))
    try: conn.close()
    except Exception: pass

def accept_clients():
    while not is_stopped:
        try:
            control_socket.settimeout(1.0)
            conn, addr = control_socket.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        threading.Thread(target=listen_to_client, args=(conn, addr), daemon=True).start()

def start_server():
    global control_socket, live_socket, is_stopped
    is_stopped = False

    control_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    control_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    control_socket.bind((HOST, CONTROL_PORT))
    control_socket.listen(5)

    live_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    live_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    live_socket.bind((HOST, LIVE_PORT))
    live_socket.listen(5)

    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "127.0.0.1"
    log(f"Server IP: {ip}   (ports {CONTROL_PORT}/{LIVE_PORT})")

    start_btn.config(state='disabled', bg="#3a3a4a")
    stop_btn.config(state='normal', bg=RED, fg="#1e1e2e")

    threading.Thread(target=accept_clients, daemon=True).start()
    threading.Thread(target=accept_uploads,  daemon=True).start()

def stop_server():
    global is_stopped
    is_stopped = True
    log("Server stopped.")

    with clients_lock:
        for c in clients:
            try: c["conn"].close()
            except Exception: pass
        clients.clear()
    with live_state_lock:
        for entry in live_streams.values():
            conn = entry.get("conn")
            if conn:
                try: conn.close()
                except Exception: pass
        live_streams.clear()

    if control_socket:
        try: control_socket.close()
        except Exception: pass
    if live_socket:
        try: live_socket.close()
        except Exception: pass

    update_counts()
    start_btn.config(state='normal', bg=ACCENT)
    stop_btn.config(state='disabled', bg="#3a3a4a", fg=FG)

#layout
tk.Label(root, text="Live Stream Server", font=FONT_H1, bg=BG, fg=ACCENT).pack(pady=(14, 4))

status_var = tk.StringVar(value="0 connected   |   0 live")
tk.Label(root, textvariable=status_var, font=FONT, bg=BG, fg=MUTED).pack()

btns = tk.Frame(root, bg=BG)
btns.pack(pady=10)
start_btn = tk.Button(btns, text="Start Server", font=FONT, bg=ACCENT, fg="#1e1e2e",
    relief='flat', cursor='hand2', padx=14, pady=6, command=start_server)
start_btn.pack(side='left', padx=5)
stop_btn = tk.Button(btns, text="Stop Server", font=FONT, bg="#3a3a4a", fg=FG,
    relief='flat', cursor='hand2', padx=14, pady=6, state='disabled', command=stop_server)
stop_btn.pack(side='left', padx=5)

log_box = scrolledtext.ScrolledText(root, height=14, width=48, font=FONT_MONO,
    bg=BOX_BG, fg=FG, relief='flat', insertbackground=FG)
log_box.pack(padx=12, pady=(4, 12), fill='both', expand=True)

log("Ready. Click Start Server.")
root.mainloop()