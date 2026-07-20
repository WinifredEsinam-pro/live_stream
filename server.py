import socket
import cv2
import numpy as np
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, ttk
from datetime import datetime
from PIL import Image, ImageTk

from protocol import (
    TYPE_TEXT, TYPE_LIVE,
    send_message, send_text, FrameReceiver,
    pack_live_payload,
    build_rtsp_response, parse_rtsp_message,
)

CONTROL_PORT = 7777
LIVE_PORT    = 7778
HOST         = '0.0.0.0'
SERVER_STREAM_ID = "SERVER"

is_stopped     = False
control_socket = None
live_socket    = None

clients      = []           
clients_lock = threading.Lock()

live_streams    = {}        
live_state_lock = threading.Lock()

live_previews     = {}      
live_preview_lock = threading.Lock()
selected_preview  = None   

server_cam          = None
server_broadcasting = False
server_paused       = False
own_preview_buf     = None
own_preview_lock    = threading.Lock()

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


client_rows = {}  
def client_status_for(label):
    with live_state_lock:
        entry = live_streams.get(label)
    if entry is None:
        return "idle"
    return "live" if entry.get("state") == "PLAY" else "setting up"

def refresh_client_row(label):
    status = client_status_for(label)
    if label in client_rows and client_tree.exists(client_rows[label]):
        client_tree.item(client_rows[label], values=(label, status),
                          tags=(status.replace(" ", "_"),))
    else:
        client_rows[label] = client_tree.insert(
            "", tk.END, values=(label, status), tags=(status.replace(" ", "_"),))

def remove_client_row(label):
    iid = client_rows.pop(label, None)
    if iid is not None:
        try:
            client_tree.delete(iid)
        except Exception:
            pass

def clear_client_rows():
    client_rows.clear()
    for item in client_tree.get_children():
        client_tree.delete(item)

def on_client_select(event=None):
    global selected_preview
    sel = client_tree.selection()
    if sel:
        selected_preview = client_tree.item(sel[0], "values")[0]

def _draw_letterboxed(target_canvas, frame, box_w, box_h):
    frame_h, frame_w = frame.shape[:2]
    if frame_w <= 0 or frame_h <= 0 or box_w <= 0 or box_h <= 0:
        return None
    scale = min(box_w / frame_w, box_h / frame_h)
    new_w = max(1, int(frame_w * scale))
    new_h = max(1, int(frame_h * scale))
    x = (box_w - new_w) // 2
    y = (box_h - new_h) // 2
    img = Image.fromarray(frame).resize((new_w, new_h), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    target_canvas.photo = photo
    target_canvas.delete("all")
    target_canvas.create_rectangle(0, 0, box_w, box_h, fill="#0d0d14", outline="")
    target_canvas.create_image(x, y, anchor='nw', image=photo)
    return photo

def update_preview_canvas():
    with live_preview_lock:
        frame = live_previews.get(selected_preview) if selected_preview else None
    w = preview_canvas.winfo_width()  or 420
    h = preview_canvas.winfo_height() or 240
    if frame is not None:
        try:
            _draw_letterboxed(preview_canvas, frame, w, h)
        except Exception:
            pass
    else:
        preview_canvas.delete("all")
        msg = f"waiting for {selected_preview}..." if selected_preview else "click a client below to preview"
        preview_canvas.create_text(w // 2, h // 2, text=msg, fill=MUTED, font=FONT)
    root.after(60, update_preview_canvas)

def update_own_canvas():
    with own_preview_lock:
        frame = own_preview_buf
    w = own_canvas.winfo_width()  or 420
    h = own_canvas.winfo_height() or 140
    if frame is not None:
        try:
            _draw_letterboxed(own_canvas, frame, w, h)
        except Exception:
            pass
    else:
        own_canvas.delete("all")
        msg = "starting camera..." if server_broadcasting else "your camera preview"
        own_canvas.create_text(w // 2, h // 2, text=msg, fill=MUTED, font=FONT)
    root.after(60, update_own_canvas)

def end_stream(stream_id, notify=True):
    with live_state_lock:
        entry = live_streams.pop(stream_id, None)
    if entry is None:
        return
    conn = entry.get("conn")
    if conn:
        try: conn.close()
        except Exception: pass
    with live_preview_lock:
        live_previews.pop(stream_id, None)
    if notify:
        broadcast_text(f"ENDED:{stream_id}")
    root.after(0, update_counts)
    root.after(0, lambda: refresh_client_row(stream_id))
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
                    np_frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if np_frame is not None:
                        rgb = cv2.cvtColor(np_frame, cv2.COLOR_BGR2RGB)
                        with live_preview_lock:
                            live_previews[stream_id] = rgb
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

def server_camera_loop():
    global own_preview_buf, server_broadcasting
    while server_broadcasting:
        ret, frame = server_cam.read()
        if not ret:
            time.sleep(0.05)
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        with own_preview_lock:
            own_preview_buf = cv2.flip(rgb, 1)  
        if not server_paused:
            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            broadcast(TYPE_LIVE, pack_live_payload(SERVER_STREAM_ID, buf.tobytes()))
        time.sleep(1 / 20)
    try: server_cam.release()
    except Exception: pass

def start_own_broadcast():
    global server_cam, server_broadcasting, server_paused
    server_cam = cv2.VideoCapture(0)
    if not server_cam.isOpened():
        log("No camera found on this machine.")
        return
    server_broadcasting = True
    server_paused = False
    broadcast_text(f"STARTED:{SERVER_STREAM_ID}")
    log("Server camera started — broadcasting to all clients.")
    own_start_btn.config(state='disabled', bg="#3a3a4a")
    own_pause_btn.config(state='normal', bg=ACCENT, fg="#1e1e2e", text="Pause")
    own_quit_btn.config(state='normal', bg=RED, fg="#1e1e2e")
    threading.Thread(target=server_camera_loop, daemon=True).start()

def toggle_own_pause():
    global server_paused
    server_paused = not server_paused
    own_pause_btn.config(text="Resume" if server_paused else "Pause")
    log("Server camera paused." if server_paused else "Server camera resumed.")

def quit_own_broadcast():
    global server_broadcasting, server_paused, own_preview_buf
    server_broadcasting = False
    server_paused = False
    own_preview_buf = None
    broadcast_text(f"ENDED:{SERVER_STREAM_ID}")
    log("Server camera stopped.")
    own_start_btn.config(state='normal', bg=ACCENT, fg="#1e1e2e")
    own_pause_btn.config(state='disabled', bg="#3a3a4a", fg=FG, text="Pause")
    own_quit_btn.config(state='disabled', bg="#3a3a4a", fg=FG)

def listen_to_client(conn, addr):
    label = f"{addr[0]}:{addr[1]}"
    info = {"conn": conn, "addr": addr, "lock": threading.Lock(), "label": label}
    with clients_lock:
        clients.append(info)
    root.after(0, update_counts)
    root.after(0, lambda: refresh_client_row(label))
    root.after(0, lambda: log(f"Connected: {label}"))

    with live_state_lock:
        active = list(live_streams.keys())
    for sid in active:
        try:
            send_text(conn, f"STARTED:{sid}", lock=info["lock"])
        except Exception:
            pass
    if server_broadcasting:
        try:
            send_text(conn, f"STARTED:{SERVER_STREAM_ID}", lock=info["lock"])
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
                    root.after(0, lambda: refresh_client_row(label))
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
                    root.after(0, lambda: refresh_client_row(label))
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
    root.after(0, lambda: remove_client_row(label))

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
    if server_broadcasting:
        quit_own_broadcast()
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
    clear_client_rows()
    with live_preview_lock:
        live_previews.clear()
    start_btn.config(state='normal', bg=ACCENT)
    stop_btn.config(state='disabled', bg="#3a3a4a", fg=FG)

#layout
root.geometry("460x760")

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

tk.Label(root, text="Your camera", font=FONT, bg=BG, fg=MUTED).pack(
    anchor='w', padx=14, pady=(6, 0))
own_canvas = tk.Canvas(root, height=140, bg="#0d0d14", highlightthickness=0)
own_canvas.pack(padx=14, pady=(2, 6), fill='both', expand=True)

own_btns = tk.Frame(root, bg=BG)
own_btns.pack(pady=(0, 10))
own_start_btn = tk.Button(own_btns, text="Start", font=FONT, bg=ACCENT, fg="#1e1e2e",
    relief='flat', cursor='hand2', padx=12, pady=5, command=start_own_broadcast)
own_start_btn.pack(side='left', padx=4)
own_pause_btn = tk.Button(own_btns, text="Pause", font=FONT, bg="#3a3a4a", fg=FG,
    relief='flat', cursor='hand2', padx=12, pady=5, state='disabled', command=toggle_own_pause)
own_pause_btn.pack(side='left', padx=4)
own_quit_btn = tk.Button(own_btns, text="Quit", font=FONT, bg="#3a3a4a", fg=FG,
    relief='flat', cursor='hand2', padx=12, pady=5, state='disabled', command=quit_own_broadcast)
own_quit_btn.pack(side='left', padx=4)

tk.Label(root, text="Preview (click a client below)", font=FONT, bg=BG, fg=MUTED).pack(anchor='w', padx=14, pady=(4, 0))
preview_canvas = tk.Canvas(root, height=220, bg="#0d0d14", highlightthickness=0)
preview_canvas.pack(padx=14, pady=(2, 8), fill='both', expand=True)

tk.Label(root, text="Connected clients", font=FONT, bg=BG, fg=MUTED).pack(anchor='w', padx=14)

tree_style = ttk.Style()
tree_style.theme_use('clam')
tree_style.configure("Client.Treeview", background=BOX_BG, fieldbackground=BOX_BG,
    foreground=FG, rowheight=22, borderwidth=0, font=FONT)
tree_style.configure("Client.Treeview.Heading", background="#262a3d", foreground=MUTED,
    font=FONT, borderwidth=0)
tree_style.map("Client.Treeview", background=[("selected", "#3a3a4a")])

client_tree = ttk.Treeview(root, style="Client.Treeview", columns=("client", "status"),
    show="headings", height=5, selectmode="browse")
client_tree.heading("client", text="CLIENT")
client_tree.heading("status", text="STATUS")
client_tree.column("client", width=220, anchor='w')
client_tree.column("status", width=140, anchor='w')
client_tree.tag_configure("live", foreground=RED)
client_tree.tag_configure("setting_up", foreground="#ffd166")
client_tree.tag_configure("idle", foreground=MUTED)
client_tree.pack(padx=14, pady=(4, 10), fill='x')
client_tree.bind("<<TreeviewSelect>>", on_client_select)

log_box = scrolledtext.ScrolledText(root, height=10, width=48, font=FONT_MONO,
    bg=BOX_BG, fg=FG, relief='flat', insertbackground=FG)
log_box.pack(padx=12, pady=(4, 12), fill='both', expand=True)

log("Ready. Click Start Server.")
update_preview_canvas()
update_own_canvas()
root.mainloop()