#!/usr/bin/env python3
"""Read Otty's review decisions from Telegram and record them in the private repo's review.json.
Approve / Hold buttons, or a reply "approve" / "hold" to a review message. After a hold, a reply under the
video or under the hold message is saved as notes for the editor. Saving review.json triggers review_apply
in the private repo. Logs never print message text or usernames."""
import base64, json, os, re, sys, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dm_inbox import Inbox
TOK = os.environ["TELEGRAM_BOT_TOKEN"].strip().removeprefix("bot").strip(); API = f"https://api.telegram.org/bot{TOK}"
PRIV = "HypeOtter/HypeOtter"
H = {"Authorization": f"Bearer {os.environ['PRIVATE_DATA_TOKEN']}", "Accept": "application/vnd.github+json"}


def get(path):
    r = requests.get(f"https://api.github.com/repos/{PRIV}/contents/{path}", headers=H, timeout=30)
    if r.status_code == 404: return None, None
    r.raise_for_status(); j = r.json()
    return base64.b64decode(j["content"]).decode(), j["sha"]


def put(path, text, sha, msg):
    body = {"message": msg, "content": base64.b64encode(text.encode()).decode(), "branch": "main",
            "committer": {"name": "HypeOtter bot", "email": "357183714+HypeOtter@users.noreply.github.com"}}
    if sha: body["sha"] = sha
    r = requests.put(f"https://api.github.com/repos/{PRIV}/contents/{path}", headers=H, json=body, timeout=30)
    r.raise_for_status()


def tg(method, **data):
    try: return requests.post(f"{API}/{method}", data=data, timeout=30).json()
    except Exception: return {}


def review(ups):
    raw, sha = get("review.json")
    if not raw: print("no review.json"); return
    d = json.loads(raw)
    if not d.get("enabled"): print("review off"); return
    items = d.setdefault("items", {})
    if not ups: print("no updates"); return
    by_msg = {mid: s for s, it in items.items() for mid in it.get("msgs", [])}
    changed = 0

    def decide(s, st, chat):
        it = items.get(s)
        if not it or it.get("status") not in ("pending", "hold"): return False
        if st == "approved" and it.get("status") == "approved": return False
        it["status"] = st
        if st == "hold": it["hold_posted"] = False
        txt = "✅ Approved. Uploading to YouTube (private) and queuing the socials." if st == "approved" else \
              "✋ On hold. Reply to this message with notes for the editor."
        r = tg("sendMessage", chat_id=chat, text=f"{txt}\n{s}")
        mid = (r.get("result") or {}).get("message_id")
        if mid: it.setdefault("msgs", []).append(mid); by_msg[mid] = s
        print("decision", s, st)
        return True

    for u in ups:
        cq = u.get("callback_query")
        if cq:
            m = re.match(r"(ok|hold):(.+)", cq.get("data", ""))
            tg("answerCallbackQuery", callback_query_id=cq["id"])
            if m:
                chat = cq["message"]["chat"]["id"]
                if decide(m.group(2), "approved" if m.group(1) == "ok" else "hold", chat):
                    changed += 1
                    if m.group(1) == "ok":
                        tg("editMessageReplyMarkup", chat_id=chat, message_id=cq["message"]["message_id"],
                           reply_markup=json.dumps({"inline_keyboard": []}))
            continue
        msg = u.get("message") or {}
        rep = (msg.get("reply_to_message") or {}).get("message_id")
        text = (msg.get("text") or "").strip()
        s = by_msg.get(rep)
        if not s or not text: continue
        w = text.lower().strip(" .!")
        if w in ("approve", "approved", "ok", "yes"):
            changed += decide(s, "approved", msg["chat"]["id"])
        elif w in ("hold", "no"):
            changed += decide(s, "hold", msg["chat"]["id"])
        elif items[s].get("status") == "hold":
            items[s].setdefault("notes", []).append(text[:1500]); changed += 1
            tg("sendMessage", chat_id=msg["chat"]["id"], text="📝 Notes saved for the editor.", reply_to_message_id=msg["message_id"])
            print("notes", s)
    if changed:
        put("review.json", json.dumps(d, indent=1), sha, "review: Otty's decisions")
    print("updates", len(ups), "changes", changed)


def main():
    off_raw, off_sha = get("social/review_offset.txt")
    off = int(off_raw.strip()) if off_raw and off_raw.strip().isdigit() else 0
    ups = requests.get(f"{API}/getUpdates", params={"offset": off, "timeout": 0,
                       "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=40).json().get("result", [])
    cfg_raw, _ = get("social/config.json")
    inbox = Inbox(tg, get, put, json.loads(cfg_raw) if cfg_raw else {})
    dm_ups = [u for u in ups if inbox.owns(u)]
    rest = [u for u in ups if not inbox.owns(u)]
    try:
        review(rest)
    except Exception as e:
        print("review step failed, keeping updates for next run:", type(e).__name__); ups = []
    try:
        inbox.run(dm_ups)
    except Exception as e:
        print("dm step failed:", type(e).__name__)
    if ups:
        off = ups[-1]["update_id"] + 1
        put("social/review_offset.txt", str(off), off_sha, "review offset")
    print("updates", len(ups), "dm", len(dm_ups))


if __name__ == "__main__":
    main()
