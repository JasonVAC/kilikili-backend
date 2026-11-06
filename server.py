#!/usr/bin/env python3
"""
KiliKili 后端 —— 给 J2ME/S60v3 客户端用的 B 站代理

客户端约定（从 KiliKili 源码推出）：
  :3000 /list?bvid=            分P列表   → 需含 "cid" 和 "part"
  :3000 /view?bvid=&cid=       视频信息   → title/desc/pic/name/mid/stat.*
  :3000 /search?keyword=&page= 搜索       → 需含 title/bvid
  :3000 /user?mid=             用户信息   → name/sign/fans/face...
  :3000 /user/video?mid=&aid=  用户投稿   → title/bvid/param
  :3232 /                      推荐       → title/bvid
  :2121 /api/playurl?bvid=&cid= 直链      → {"code":0,"url":"..."}
  :4000 /api/download?bvid=&cid= 开始转码 → {"code":0,"taskId":...}
  :4000 /api/status?taskId=     查状态    → {"code":0,"status":...,"queueLength":0}
  :4000 /api/output/<id>_240p.mp4 下载转码结果

客户端解析很朴素（找 "key" 后取下一个引号内容 / 冒号后的数字），
所以直接把 B 站原始 JSON 透传基本就能用。
"""
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*"}
COOKIE = {"v": ""}
COOKIE_LOCK = threading.Lock()


def refresh_cookie():
    """访问首页拿 buvid3 等 Cookie（搜索接口必须）"""
    req = urllib.request.Request("https://www.bilibili.com/", headers={
        "User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=20) as r:
        pass
    return None


def get_cookie(force=False):
    with COOKIE_LOCK:
        if COOKIE["v"] and not force:
            return COOKIE["v"]
        try:
            import http.cookiejar as cj
            jar = cj.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            req = urllib.request.Request("https://www.bilibili.com/",
                                         headers={"User-Agent": UA})
            op.open(req, timeout=20).read(2000)
            ck = "; ".join("%s=%s" % (c.name, c.value) for c in jar)
            COOKIE["v"] = ck
        except Exception:
            pass
        return COOKIE["v"]


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(BASE_DIR, "output")
JOBS = {}
LOCK = threading.Lock()

os.makedirs(OUTDIR, exist_ok=True)


def http_get(url, extra=None, timeout=25, binary=False):
    h = dict(HDRS)
    ck = get_cookie()
    if ck:
        h["Cookie"] = ck
    if extra:
        h.update(extra)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def bili(path, params):
    q = urllib.parse.urlencode(params)
    return json.loads(http_get("https://api.bilibili.com" + path + "?" + q))


def search_kw(keyword, page=1):
    try:
        d = bili("/x/web-interface/search/type",
                 {"search_type": "video", "keyword": keyword, "page": page})
    except Exception:
        d = {"code": -1}
    if d.get("code") != 0:
        get_cookie(force=True)
        d = bili("/x/web-interface/search/type",
                 {"search_type": "video", "keyword": keyword, "page": page})
    return d


def playurl(bvid, cid):
    return bili("/x/player/playurl",
                {"bvid": bvid, "cid": cid, "qn": 16, "fnval": 1, "platform": "html5"})


def transcode_worker(task_id, bvid, cid):
    try:
        info = playurl(bvid, cid)
        durl = info.get("data", {}).get("durl") or []
        if not durl:
            raise RuntimeError("no durl")
        src = durl[0]["url"]
        out = os.path.join(OUTDIR, "%s_240p.mp4" % task_id)
        tmp = out + ".src.mp4"
        with open(tmp, "wb") as f:
            # 分段写，避免整段进内存
            req = urllib.request.Request(src, headers=HDRS)
            with urllib.request.urlopen(req, timeout=60) as r:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
        cmd = ["ffmpeg", "-y", "-i", tmp,
               "-vf", "scale=256:144:force_original_aspect_ratio=decrease",
               "-c:v", "libx264", "-profile:v", "baseline", "-level", "2.1",
               "-b:v", "72k", "-maxrate", "80k", "-bufsize", "160k",
               "-r", "12", "-g", "24",
               "-c:a", "aac", "-b:a", "24k", "-ac", "1", "-ar", "22050",
               "-movflags", "+faststart", out]
        subprocess.run(cmd, capture_output=True, timeout=900)
        try:
            os.remove(tmp)
        except OSError:
            pass
        with LOCK:
            JOBS[task_id] = {"state": "done", "file": out}
    except Exception as e:
        with LOCK:
            JOBS[task_id] = {"state": "error", "msg": "%s: %s" % (type(e).__name__, e)}



def ensure_transcoded(bvid, cid):
    """确保 240p 文件存在，没有就同步转码"""
    tid = cid or bvid
    fn = os.path.join(OUTDIR, "%s_240p.mp4" % tid)
    if os.path.exists(fn) and os.path.getsize(fn) > 1024:
        return fn
    transcode_worker(tid, bvid, cid)
    return fn if os.path.exists(fn) else None



STREAM_HOST_FILE = os.path.join(BASE_DIR, "current_host.txt")


def stream_host(default=""):
    """视频流走 cpolar（快、无限时），域名由 host-watcher 更新"""
    try:
        h = open(STREAM_HOST_FILE, encoding="utf-8").read().strip()
        if h:
            return h
    except Exception:
        pass
    return default


def page_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def serve_page(body):
    data = body.encode("utf-8")
    return {"code": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    port_role = "info"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s %s UA=%s\n" % (self.port_role, self.address_string(), fmt % args, self.headers.get("User-Agent","-")[:70]))

    def send_raw(self, text, code=200, ctype="application/json; charset=utf-8"):
        data = text.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_obj(self, obj):
        self.send_raw(page_json(obj))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        role = self.port_role

        if role == "single":
            try:
                if _route_single(self, p, q):
                    return
            except Exception as e:
                self.send_obj({"code": -1, "message": "%s: %s" % (type(e).__name__, e)})
                return

        try:
            if role == "rcmd":
                d = bili("/x/web-interface/wbi/index/top/feed/rcmd", {})
                self.send_obj(d)
                return

            if role == "dl":
                if p.startswith("/api/output/"):
                    name = os.path.basename(p)
                    fn = os.path.join(OUTDIR, name)
                    if not os.path.exists(fn):
                        self.send_raw("not found", 404, "text/plain")
                        return
                    data = open(fn, "rb").read()
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if p.startswith("/api/playurl"):
                    bvid = (q.get("bvid") or [""])[0]
                    cid = (q.get("cid") or [""])[0]
                    d = playurl(bvid, cid)
                    durl = d.get("data", {}).get("durl") or []
                    url = durl[0]["url"] if durl else ""
                    self.send_obj({"code": 0 if url else -1, "url": url})
                    return
                if p.startswith("/api/download"):
                    bvid = (q.get("bvid") or [""])[0]
                    cid = (q.get("cid") or [""])[0]
                    tid = cid or bvid
                    with LOCK:
                        JOBS[tid] = {"state": "running", "file": ""}
                    threading.Thread(target=transcode_worker, args=(tid, bvid, cid), daemon=True).start()
                    self.send_obj({"code": 0, "taskId": tid})
                    return
                if p.startswith("/api/status"):
                    tid = (q.get("taskId") or [""])[0]
                    with LOCK:
                        job = JOBS.get(tid)
                    self.send_obj({"code": 0, "status": (job or {}).get("state", "none"),
                                   "queueLength": 0, "taskId": tid})
                    return
                self.send_obj({"code": -1, "message": "unknown"})
                return

            # 信息端口 :3000
            if p.startswith("/list"):
                bvid = (q.get("bvid") or [""])[0]
                self.send_obj(bili("/x/player/pagelist", {"bvid": bvid}))
                return
            if p.startswith("/view"):
                bvid = (q.get("bvid") or [""])[0]
                self.send_obj(bili("/x/web-interface/view", {"bvid": bvid}))
                return
            if p.startswith("/search"):
                kw = (q.get("keyword") or [""])[0]
                pg = int((q.get("page") or ["1"])[0])
                self.send_obj(search_kw(kw, pg))
                return
            if p.startswith("/user/video"):
                mid = (q.get("mid") or [""])[0]
                self.send_obj(bili("/x/space/wbi/arc/search",
                                   {"mid": mid, "ps": 20, "pn": 1}))
                return
            if p.startswith("/user"):
                mid = (q.get("mid") or [""])[0]
                self.send_obj(bili("/x/web-interface/card", {"mid": mid}))
                return
            self.send_obj({"code": 0, "message": "kilikili backend"})
        except Exception as e:
            self.send_obj({"code": -1, "message": "%s: %s" % (type(e).__name__, e)})



def _route_single(handler, p, q):
    """单端口模式：按路径分发到 info / rcmd / dl"""
    if p.startswith("/ping"):
        handler.send_raw("kilikili backend OK", 200, "text/plain; charset=utf-8")
        return True
    if p.startswith("/rcmd"):
        d = bili("/x/web-interface/wbi/index/top/feed/rcmd", {})
        handler.send_obj(d); return True
    if p.startswith("/stream"):
        bvid = (q.get("bvid") or [""])[0]
        cid = (q.get("cid") or [""])[0]
        tid = cid or bvid
        fn = os.path.join(OUTDIR, "%s_240p.mp4" % tid)
        # 已有转码产物 → 直接给 240p
        if os.path.exists(fn) and os.path.getsize(fn) > 1024:
            data = open(fn, "rb").read()
            handler.send_response(200)
            handler.send_header("Content-Type", "video/mp4")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return True
        # 没有 → 先等后台转码最多 25 秒
        if not (os.path.exists(fn) and os.path.getsize(fn) > 1024):
            with LOCK:
                if not JOBS.get(tid):
                    JOBS[tid] = {"state": "running", "file": ""}
                    threading.Thread(target=transcode_worker, args=(tid, bvid, cid), daemon=True).start()
            import time as _t
            for _ in range(50):
                _t.sleep(0.5)
                with LOCK:
                    st = (JOBS.get(tid) or {}).get("state")
                if st == "done" and os.path.exists(fn) and os.path.getsize(fn) > 1024:
                    data = open(fn, "rb").read()
                    handler.send_response(200)
                    handler.send_header("Content-Type", "video/mp4")
                    handler.send_header("Content-Length", str(len(data)))
                    handler.end_headers()
                    handler.wfile.write(data)
                    return True
                if st == "error":
                    break
        d = playurl(bvid, cid)
        durl = d.get("data", {}).get("durl") or []
        if not durl:
            handler.send_raw("no source", 502, "text/plain")
            return True
        src = durl[0]["url"]
        with LOCK:
            if not JOBS.get(tid):
                JOBS[tid] = {"state": "running", "file": ""}
                threading.Thread(target=transcode_worker, args=(tid, bvid, cid), daemon=True).start()
        req = urllib.request.Request(src, headers=HDRS)
        try:
            up = urllib.request.urlopen(req, timeout=60)
        except Exception as e:
            handler.send_raw("source error: %s" % e, 502, "text/plain")
            return True
        length = up.headers.get("Content-Length")
        handler.send_response(200)
        handler.send_header("Content-Type", "video/mp4")
        if length:
            handler.send_header("Content-Length", length)
        handler.end_headers()
        try:
            while True:
                chunk = up.read(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
        except Exception:
            pass
        return True
    if p.startswith("/playurl"):
        bvid=(q.get("bvid") or [""])[0]; cid=(q.get("cid") or [""])[0]
        host = stream_host(handler.headers.get("Host") or "127.0.0.1:8083")
        tid = cid or bvid
        fn = os.path.join(OUTDIR, "%s_240p.mp4" % tid)
        if not (os.path.exists(fn) and os.path.getsize(fn) > 1024):
            with LOCK:
                if not JOBS.get(tid):
                    JOBS[tid] = {"state": "running", "file": ""}
                    threading.Thread(target=transcode_worker, args=(tid, bvid, cid), daemon=True).start()
        url = "http://%s/stream?bvid=%s&cid=%s" % (host, bvid, cid)
        handler.send_obj({"code":0,"url":url}); return True
    if p.startswith("/download"):
        bvid=(q.get("bvid") or [""])[0]; cid=(q.get("cid") or [""])[0]
        tid=cid or bvid
        with LOCK: JOBS[tid]={"state":"running","file":""}
        threading.Thread(target=transcode_worker,args=(tid,bvid,cid),daemon=True).start()
        handler.send_obj({"code":0,"taskId":tid}); return True
    if p.startswith("/status"):
        tid=(q.get("taskId") or [""])[0]
        with LOCK: job=JOBS.get(tid)
        handler.send_obj({"code":0,"status":(job or {}).get("state","none"),"queueLength":0,"taskId":tid}); return True
    if p.startswith("/output/"):
        name=os.path.basename(p); fn=os.path.join(OUTDIR,name)
        if not os.path.exists(fn):
            handler.send_raw("not found",404,"text/plain"); return True
        data=open(fn,"rb").read()
        handler.send_response(200)
        handler.send_header("Content-Type","video/mp4")
        handler.send_header("Content-Length",str(len(data)))
        handler.end_headers(); handler.wfile.write(data); return True
    return False

def make(role, port):
    cls = type("H_" + role, (Handler,), {"port_role": role})
    srv = ThreadingMixIn
    httpd = HTTPServer(("0.0.0.0", port), cls)
    httpd.daemon_threads = True
    return httpd


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "single":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8083
        h = make("single", port)
        print("single-port mode on %d" % port, flush=True)
        h.serve_forever()
        raise SystemExit
    ports = [(3232, "rcmd"), (2121, "dl"), (4000, "dl"), (3000, "info")]
    servers = []
    for port, role in ports:
        h = make(role, port)
        servers.append(h)
        threading.Thread(target=h.serve_forever, daemon=True).start()
        print("listening %d (%s)" % (port, role), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
