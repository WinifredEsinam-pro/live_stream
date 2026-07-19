Live-only video streaming: any client can go live, any client can watch.

- live_protocol.py
- live_server.py
- live_client.py

(keep all three in the same folder)

## Setup & Run


python3 --version                          # confirm Python 3 is installed
pip install opencv-python numpy pillow     # install dependencies
sudo apt install python3-tk                # Linux only, if tkinter errors out
python3 live_server.py                     # one person runs this
python3 live_client.py                     # everyone runs this


Enter the server's IP in the client (127.0.0.1 if same machine), click Connect.
Click "Go Live" to broadcast, or pick someone from the dropdown and click "Watch".

## Protocol
Session control uses RTSP request/response messages (SETUP, PLAY, TEARDOWN
with CSeq/Session headers, per RFC 2326). Video frames themselves are sent
over a simple custom TCP connection, not RTP, since this is a live camera
feed rather than a stored file.