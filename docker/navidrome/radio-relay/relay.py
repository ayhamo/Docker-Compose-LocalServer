"""Re-streams HLS radio stations as plain MP3 so any Subsonic client can play them.

One ffmpeg process per listener, started on connect and killed on disconnect.
Add a station by adding a line to STATIONS and restarting the container.
"""
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIONS = {
    "kuwait-general": "https://kwtkrdota.cdn.mangomolo.com/k1rdo/k1rdo.stream_aac/chunklist.m3u8",
    "kuwait-program2": "https://kwtkrdota.cdn.mangomolo.com/k2rdo/k2rdo.stream_aac/chunklist.m3u8",
    "kuwait-fm": "https://kwtkrdota.cdn.mangomolo.com/kfmrdo/kfmrdo.stream_aac/chunklist.m3u8",
    "kuwait-quran": "https://kwtkrdota.cdn.mangomolo.com/qrrdo/qrrdo.stream_aac/chunklist.m3u8",
    "kuwait-thikr": "https://kwtkrdota.cdn.mangomolo.com/daraerdo/daraerdo.stream_aac/chunklist.m3u8",
    "marina-fm": "http://ffs3.gulfsat.com/MARINA-FM-904/tracks-a1/mono.m3u8",
}


class Relay(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.strip("/").split("?")[0].removesuffix(".mp3")
        if self.path == "/health":
            return self._reply(200, b"ok")
        if name not in STATIONS:
            return self._reply(404, ("stations: " + ", ".join(f"/{s}.mp3" for s in STATIONS)).encode())

        ffmpeg = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
             "-i", STATIONS[name], "-vn", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1"],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
        )
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("icy-name", name)
        self.end_headers()
        # A listener that vanished without closing (no FIN/RST) would block the write for ~15 min
        # and keep its ffmpeg running; give up after 30 s of no progress instead.
        self.connection.settimeout(30)
        try:
            while chunk := ffmpeg.stdout.read(8192):
                self.wfile.write(chunk)
        except OSError:   # BrokenPipe, ConnectionReset, timeout
            pass
        finally:
            ffmpeg.kill()
            ffmpeg.wait()

    def _reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")


ThreadingHTTPServer(("0.0.0.0", 8080), Relay).serve_forever()
