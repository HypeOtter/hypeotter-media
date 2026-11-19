#!/usr/bin/env python3
"""Zero-token native video poster for HypeOtter.
pick : choose the next public YouTube video that has approved/<slug>/final.mp4 in the private data repo,
       write plan.json (+ GitHub outputs). The workflow then fetches the file and publishes it on GitHub Pages.
post : post the hosted file natively: X, Facebook (Reel) and Threads via Buffer; Instagram Reel via the
       Graph API (file upload, no public link needed); TikTok inbox draft (file upload); Snapchat kit to Telegram.
       Waits for Buffer to finish fetching, then records results in social/native.json and marks
       social/shared.json so the link sharer doesn't post the same video's link again.
No LLM. Public logs print counts, platforms and status only.
Env: DATA_DIR (private data checkout), BUFFER_TOKEN, META_PAGE_TOKEN, IG_USER_ID, TIKTOK_CLIENT_KEY,
TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN, TELEGRAM_BOT_TOKEN, DRY_RUN, FORCE, SLUG,
ONLY (comma list of platforms), MEDIA_URL (set by the workflow), VIDEO_FILE."""
import json, os, re, sys, time, random, struct, secrets, datetime as dt
import urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = os.environ.get("DATA_DIR", "data")
CFG_ALL = json.load(open(os.path.join(ROOT, "social", "config.json")))
CFG = {
    "enabled": False, "platforms": ["twitter", "facebook", "threads", "instagram", "tiktok", "snapchat"],
    "backfill_buffer": False, "kinds": ["short"], "post_hours_et": [9, 22], "min_gap_minutes": 180,
    "max_seconds": {"twitter": 140, "facebook": 90, "threads": 300, "instagram": 900, "tiktok": 600},
    "link_reply": True, "max_tries": 3,
} | CFG_ALL.get("native", {})
NATIVE_P = os.path.join(ROOT, "social", "native.json")
SHARED_P = os.path.join(ROOT, "social", "shared.json")
ET_TZ = ZoneInfo("America/New_York")
DRY = os.environ.get("DRY_RUN") == "1"
FORCE = os.environ.get("FORCE") == "1"
ONLY = [p for p in os.environ.get("ONLY", "").split(",") if p]
GRAPH = "https://graph.facebook.com/v23.0"
BUFFER_SVC = ("twitter", "facebook", "threads")

def http(url, data=None, headers=None, method=None, timeout=60):
    h = {"User-Agent": "HypeOtterBot"} | (headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def jload(p, d):
    return json.load(open(p)) if os.path.exists(p) else d

def jsave(p, o):
    json.dump(o, open(p, "w"), indent=1, sort_keys=True)

def out(k, v):
    if os.environ.get("GITHUB_OUTPUT"):
        open(os.environ["GITHUB_OUTPUT"], "a").write(f"{k}={v}\n")

def is_public(vid):
    st, _ = http(f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={vid}")
    return st == 200

def rss_ids():
    st, body = http(f"https://www.youtube.com/feeds/videos.xml?channel_id={CFG_ALL['channel_id']}")
    if st != 200:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    return [e.find("yt:videoId", ns).text for e in ET.fromstring(body).findall("a:entry", ns)]

def uploaded():
    up = json.load(open(os.path.join(ROOT, "yt_uploaded.json")))
    return {slug: vid for slug, vid in up.items() if not slug.startswith("_") and isinstance(vid, str)}

def manifest(slug):
    return jload(os.path.join(ROOT, "approved", slug, "manifest.json"), None)

# ---------------- pick ----------------
def pick():
    now = dt.datetime.now(ET_TZ)
    nat = jload(NATIVE_P, {"videos": {}, "last_post_at": None})
    want = os.environ.get("SLUG", "").strip()
    if not want and not FORCE:
        if not CFG["enabled"]:
            print("native posting is off (social/config.json native.enabled)"); return out("slug", "")
        lo, hi = CFG["post_hours_et"]
        if not lo <= now.hour <= hi:
            print("outside posting hours"); return out("slug", "")
        if nat.get("last_post_at"):
            gap = (now - dt.datetime.fromisoformat(nat["last_post_at"])).total_seconds() / 60
            if gap < CFG["min_gap_minutes"]:
                print(f"gap {gap:.0f} min"); return out("slug", "")
    up = uploaded()
    by_vid = {v: s for s, v in up.items()}
    skip = CFG_ALL.get("skip_video_ids", {})
    skip_slugs = set(CFG.get("skip_slugs", []))
    sent = [x for x in jload(os.path.join(ROOT, "sent.json"), []) if isinstance(x, str)]
    order = [by_vid[v] for v in rss_ids() if v in by_vid] + list(reversed(list(up)))
    if CFG.get("include_unpublished"):
        order += [x for x in sent if x not in up]  # delivered to Telegram, never on YouTube
    later = []
    for slug in dict.fromkeys(order):
        if want and slug != want:
            continue
        vid = up.get(slug)
        man = manifest(slug)
        if not man or (vid and vid in skip) or slug in skip_slugs:
            continue
        if not want and man.get("kind", "short") not in CFG["kinds"]:
            continue
        key = vid or "slug:" + slug
        rec = nat["videos"].get(key, {})
        todo = [p for p in targets(key) if not rec.get(p, {}).get("ok") and rec.get(p, {}).get("tries", 0) < CFG["max_tries"]]
        if not todo:
            continue
        public = bool(vid) and is_public(vid)
        if not public:
            # unpublished = delivered to Telegram but not public on YouTube; posted after public ones, no YouTube link
            if (CFG.get("include_unpublished") and (want or slug in sent)) or want:
                later.append((slug, key, todo))
            continue
        return choose(slug, key, todo, True)
    if later:
        return choose(*later[0], False)
    print("nothing to post natively"); out("slug", "")

def choose(slug, key, todo, public):
    plan = {"slug": slug, "vid": key, "todo": todo, "public": public, "token": secrets.token_hex(8)}
    jsave("plan.json", plan)
    print("picked 1 video for", ",".join(todo), "(public on YouTube)" if public else "(not on YouTube yet)")
    out("slug", slug); out("token", plan["token"])

def targets(vid):
    plats = ONLY or CFG["platforms"]
    if CFG["backfill_buffer"] or ONLY:
        return plats
    shared = jload(SHARED_P, {"videos": {}}).get("videos", {}).get(vid, {})
    # a video whose link already went out on X/FB/Threads only gets native posts on the other platforms
    return [p for p in plats if not (p in BUFFER_SVC and shared.get(p, {}).get("ok"))]

# ---------------- captions ----------------
def clean(t):
    return re.sub(r"\s+", " ", re.sub(r"#\w+", "", t or "")).strip(" |-")

def bits(man):
    title = clean(man.get("title"))
    q = re.sub(r"https?://\S+", "", man.get("pinned_comment") or "").strip()
    if "?" not in q:
        q = random.choice(["What do you think?", "Would you have done the same?", "Rate this 1 to 10."])
    tags = [h for h in re.findall(r"#(\w+)", man.get("description", "")) if h.lower() not in ("shorts", "short", "hypeotter")]
    tags = ["#" + h for h in dict.fromkeys(tags)][:3]
    credit = ""
    m = re.search(r"Credit:([^\n]+)", man.get("description", ""))
    if m:
        credit = "Credit:" + m.group(1).split(".")[0].strip()
    return title, q, tags, credit

def caption(p, man, yt):
    title, q, tags, credit = bits(man)
    t = " ".join(tags + ["#hypeotter"])
    if p == "twitter":
        txt = f"{title} 🦦\n\n{q}\n\n{t}"
        while len(txt) > 270 and tags:
            tags = tags[:-1]; txt = f"{title} 🦦\n\n{q}\n\n{' '.join(tags + ['#hypeotter'])}"
        return txt[:280]
    if p == "threads":
        return f"{title} 🦦\n\n{q}\n\nfollow for more {t}"[:500]
    cr = (credit + "\n") if credit else ""
    if p == "facebook":
        return f"{title} 🦦\n\n{q}\n\n{cr}Follow the Page for more and subscribe on YouTube 🔔\n\n{t}"
    if p == "instagram":
        return f"{title} 🦦\n\n{q}\n\n{cr}Follow @hypeotterclips for more. Full videos: link in bio 🔔\n\n{t} #reels"[:2200]
    if p == "tiktok":
        return f"{title} 🦦 {q} {t} #fyp"[:2200]
    if p == "snapchat":
        return f"{title} 🦦 {q} {' '.join(tags[:2])} #hypeotter"
    return title

def link_line(p, yt):
    return {"twitter": f"Full video on YouTube ▶️ {yt}", "threads": f"watch the full thing ▶️ {yt}",
            "facebook": f"▶️ Watch on YouTube: {yt}"}.get(p, yt)

def mp4_seconds(path):
    with open(path, "rb") as f:
        data = f.read()
    i = data.find(b"mvhd")
    if i < 0:
        return 0
    ver = data[i + 4]
    if ver == 1:
        ts, dur = struct.unpack(">IQ", data[i + 24:i + 36])
    else:
        ts, dur = struct.unpack(">II", data[i + 16:i + 24])
    return dur / ts if ts else 0

# ---------------- Buffer ----------------
def gql(q):
    st, body = http("https://api.buffer.com", json.dumps({"query": q}).encode(),
                    {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["BUFFER_TOKEN"]})
    r = json.loads(body or b"{}")
    if r.get("errors"):
        raise RuntimeError(str(r["errors"])[:300])
    return r["data"]

def buffer_channels():
    ch = {}
    for org in gql("query { account { organizations { id } } }")["account"]["organizations"]:
        for c in gql('query { channels(input: { organizationId: %s }) { id service } }' % json.dumps(org["id"]))["channels"]:
            ch.setdefault(c["service"], c["id"])
    return ch

def buffer_video(p, chan, text, url, link, secs):
    asset = '[{ video: { url: %s } }]' % json.dumps(url)
    meta = ""
    if p == "facebook":
        # firstComment needs a paid Buffer plan, so the link rides in the caption instead
        meta = 'metadata: { facebook: { type: %s } },' % ("reel" if secs <= CFG["max_seconds"]["facebook"] else "post")
        if link and link not in text:
            text = text + "\n\n" + link
    elif link and p in ("twitter", "threads"):
        thread = '[{ text: %s, assets: %s }, { text: %s }]' % (json.dumps(text), asset, json.dumps(link))
        meta = 'metadata: { %s: { thread: %s } },' % (p, thread)
    q = ('mutation { createPost(input: { text: %s, channelId: %s, assets: %s, %s schedulingType: automatic, mode: shareNow }) '
         '{ ... on PostActionSuccess { post { id } } ... on MutationError { message } } }') % (json.dumps(text), json.dumps(chan), asset, meta)
    r = gql(q)["createPost"]
    if r.get("post"):
        return True, r["post"]["id"]
    return False, r.get("message", "error")[:200]

def buffer_wait(pid, limit=1200):
    t0 = time.time()
    while time.time() - t0 < limit:
        p = gql('query { post(input: { id: %s }) { status externalLink error { message } } }' % json.dumps(pid))["post"]
        if p["status"] in ("sent", "error"):
            return p["status"] == "sent", p.get("externalLink") or (p.get("error") or {}).get("message", "")
        time.sleep(30)
    return False, "timeout waiting for Buffer"

# ---------------- Instagram (Graph API, resumable file upload) ----------------
def instagram(path, text):
    tok, ig = os.environ["META_PAGE_TOKEN"], os.environ["IG_USER_ID"]
    st, b = http(f"{GRAPH}/{ig}/media", urllib.parse.urlencode({"media_type": "REELS", "upload_type": "resumable",
                 "caption": text, "share_to_feed": "true", "access_token": tok}).encode())
    r = json.loads(b)
    if "id" not in r:
        return False, str(r.get("error", r))[:200]
    cid, size = r["id"], os.path.getsize(path)
    st, b = http(r["uri"], open(path, "rb").read(), {"Authorization": "OAuth " + tok, "offset": "0", "file_size": str(size)}, timeout=600)
    if st != 200:
        return False, f"upload {st}"
    for _ in range(60):
        st, b = http(f"{GRAPH}/{cid}?fields=status_code&access_token={tok}")
        s = json.loads(b).get("status_code")
        if s == "FINISHED":
            break
        if s in ("ERROR", "EXPIRED"):
            return False, "processing " + s
        time.sleep(10)
    st, b = http(f"{GRAPH}/{ig}/media_publish", urllib.parse.urlencode({"creation_id": cid, "access_token": tok}).encode())
    r = json.loads(b)
    return ("id" in r), r.get("id") or str(r.get("error", r))[:200]

# ---------------- TikTok (inbox draft, file upload) ----------------
TT_AUTH = os.path.join(ROOT, "social", "tiktok_auth.json")  # private repo only; never printed

def tiktok_ready():
    return bool(os.environ.get("TIKTOK_CLIENT_SECRET")) and (
        os.path.exists(TT_AUTH) or bool(os.environ.get("TIKTOK_REFRESH_TOKEN")))

def tiktok(path):
    auth = jload(TT_AUTH, {})
    rt = auth.get("refresh_token") or os.environ.get("TIKTOK_REFRESH_TOKEN", "")
    st, b = http("https://open.tiktokapis.com/v2/oauth/token/", urllib.parse.urlencode({
        "client_key": os.environ["TIKTOK_CLIENT_KEY"], "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
        "grant_type": "refresh_token", "refresh_token": rt}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"})
    d0 = json.loads(b or "{}")
    tok = d0.get("access_token")
    if not tok:
        return False, "token refresh failed"
    if d0.get("refresh_token") and d0["refresh_token"] != rt:
        jsave(TT_AUTH, {"refresh_token": d0["refresh_token"], "open_id": d0.get("open_id", auth.get("open_id", "")),
                        "scope": d0.get("scope", auth.get("scope", "")), "updated": time.strftime("%Y-%m-%d")})
    size = os.path.getsize(path)
    chunk = size if size <= 64 * 2**20 else 10 * 2**20
    n = max(1, size // chunk)
    st, b = http("https://open.tiktokapis.com/v2/post/publish/inbox/video/init/", json.dumps({"source_info": {
        "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": n}}).encode(),
        {"Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=UTF-8"})
    d = json.loads(b).get("data", {})
    if not d.get("upload_url"):
        return False, str(json.loads(b).get("error", {}).get("code", st))
    with open(path, "rb") as f:
        for i in range(n):
            start = i * chunk
            part = f.read(chunk if i < n - 1 else size - start)
            end = start + len(part) - 1
            st, _ = http(d["upload_url"], part, {"Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{size}"}, "PUT", 600)
            if st not in (200, 201, 206):
                return False, f"chunk {i} {st}"
    return True, d.get("publish_id", "draft")

# ---------------- Snapchat kit ----------------
def snapchat(text, slug):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID") or CFG.get("telegram_chat_id")
    if not tok or not chat:
        return False, "no telegram secret"
    msg = f"Snapchat kit: {slug}\nVideo is in this chat already (or approved/{slug}/final.mp4).\nCaption:\n{text}"
    st, b = http(f"https://api.telegram.org/bot{tok}/sendMessage", urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode())
    return st == 200, "sent to Telegram" if st == 200 else f"telegram {st}"

# ---------------- post ----------------
def post():
    plan = json.load(open("plan.json"))
    slug, vid, todo = plan["slug"], plan["vid"], plan["todo"]
    man = manifest(slug)
    public = plan.get("public", True)
    yt = f"https://youtu.be/{vid}" if public else ""
    path, url = os.environ.get("VIDEO_FILE", "video.mp4"), os.environ.get("MEDIA_URL", "")
    secs = mp4_seconds(path)
    now = dt.datetime.now(ET_TZ).isoformat(timespec="minutes")
    nat = jload(NATIVE_P, {"videos": {}, "last_post_at": None})
    shared = jload(SHARED_P, {"videos": {}, "last_post_at": None})
    rec = nat["videos"].setdefault(vid, {"slug": slug})
    print(f"video {secs:.0f}s, platforms: {','.join(todo)}")
    chans = {}
    if not DRY and any(p in BUFFER_SVC for p in todo) and os.environ.get("BUFFER_TOKEN"):
        chans = buffer_channels()
    pending = {}
    for p in todo:
        text = caption(p, man, yt)
        link = link_line(p, yt) if CFG["link_reply"] and yt else ""
        limit = CFG["max_seconds"].get(p)
        if limit and secs > limit and p != "facebook":
            res = (False, f"too long for {p} ({secs:.0f}s > {limit}s)"); rec[p] = {"ok": False, "tries": 99, "ref": res[1], "at": now}
            print(p, "SKIP", res[1]); continue
        if DRY:
            print(f"--- {p} (dry run)\n{text}\n{('+ ' + link) if link and p in BUFFER_SVC else ''}"); continue
        try:
            if p in BUFFER_SVC:
                if p not in chans:
                    res = (False, "no Buffer channel")
                else:
                    ok, pid = buffer_video(p, chans[p], text, url, link, secs)
                    if ok:
                        pending[p] = pid; continue
                    res = (False, pid)
            elif p == "instagram":
                res = instagram(path, text) if os.environ.get("META_PAGE_TOKEN") and os.environ.get("IG_USER_ID") else (False, "not set up")
            elif p == "tiktok":
                res = tiktok(path) if tiktok_ready() else (False, "not set up")
            elif p == "snapchat":
                res = snapchat(text, slug)
            else:
                res = (False, "unknown platform")
        except Exception as ex:
            res = (False, str(ex)[:200])
        record(rec, p, res, now)
    for p, pid in pending.items():
        try:
            res = buffer_wait(pid)
        except Exception as ex:
            res = (False, str(ex)[:200])
        record(rec, p, res, now)
        if res[0] and not vid.startswith("slug:"):
            shared["videos"].setdefault(vid, {})[p] = {"ok": True, "ref": "native " + str(res[1])[:200], "at": now}
    if not DRY:
        nat["last_post_at"] = now
        jsave(NATIVE_P, nat); jsave(SHARED_P, shared)
    if DRY:
        print("dry run: nothing posted"); return
    fails = [p for p in todo if not rec.get(p, {}).get("ok")]
    print("done:", len(todo) - len(fails), "ok,", len(fails), "not posted", ("(" + ",".join(fails) + ")") if fails else "")

def record(rec, p, res, now):
    ok, ref = res
    r = rec.setdefault(p, {})
    r["tries"] = r.get("tries", 0) + (0 if ok else 1)
    r.update({"ok": ok, "ref": str(ref)[:200], "at": now})
    print(p, "OK" if ok else "FAILED", "" if ok else str(ref)[:120])

if __name__ == "__main__":
    {"pick": pick, "post": post}[sys.argv[1]]()
