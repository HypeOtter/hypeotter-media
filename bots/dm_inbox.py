#!/usr/bin/env python3
"""Social DM inbox -> Otty's Telegram chat, with buttons. Zero Hark tokens.\n\nPolling (new DMs, hold reminders): hypeotter-media dm_poll.yml every 5 min, 8 am-11 pm ET.\nButton taps and typed replies: the Telegram webhook (Apps Script) -> repository_dispatch tg_update -> private repo dm_hook.yml\n-> `python3 bots/dm_inbox.py hook` (instant).

Every new DM becomes one Telegram alert with four buttons:
  Manage        you handle it yourself; the alert gets an "Open inbox" button and stops nagging
  Respond once  the bot sends ONE short Otty line (rotating list, never the same twice in a row) and closes it
  Hold          parks it; the bot re-alerts you after dm.hold_hours (default 12, inside Meta's 24 h reply window)
  Custom        the bot opens a reply box in Telegram; whatever you type is sent as the DM reply
Replying straight to an alert message with text also sends that text as the reply.

Sources: Facebook Page Messenger + Instagram DMs via Meta Graph API (free; needs secrets META_PAGE_TOKEN with
pages_messaging + instagram_manage_messages, and IG_USER_ID for Instagram). X, Threads, TikTok, YouTube have no
free DM API, so they are not polled here. State: private repo social/dm_state.json. Logs never print DM text or names."""
import base64, copy, datetime as dt, json, os, random, re, sys, time, requests

GRAPH = "https://graph.facebook.com/v23.0"
STATE = "social/dm_state.json"
LABEL = {"facebook": "Facebook", "instagram": "Instagram", "test": "Test"}
INBOX = {"facebook": "https://business.facebook.com/latest/inbox/all",
         "instagram": "https://www.instagram.com/direct/inbox/",
         "test": "https://business.facebook.com/latest/inbox/all"}
FLAG = re.compile(r"sponsor|collab|business|promo|paid|pay you|price|rate|\$|money|deal|copyright|dmca|strike|lawyer|"
                  r"legal|claim|stolen|reupload|press|interview|email me|contact", re.I)
REPLIES = ["Got it, Otty's on it 👀", "Seen it. I'll get back to you soon", "Appreciate you. Reply coming soon",
           "Message received, hang tight", "Otty saw this. Back to you shortly", "Thanks for reaching out, more soon",
           "Noted! I'll circle back soon", "Love this. Give me a bit"]


PRIV = "HypeOtter/HypeOtter"


def _h():
    return {"Authorization": f"Bearer {os.environ['PRIVATE_DATA_TOKEN']}", "Accept": "application/vnd.github+json"}


def gh_get(path):
    r = requests.get(f"https://api.github.com/repos/{PRIV}/contents/{path}", headers=_h(), timeout=30)
    if r.status_code == 404: return None, None
    r.raise_for_status(); j = r.json()
    return base64.b64decode(j["content"]).decode(), j["sha"]


def gh_put(path, text, sha, msg):
    body = {"message": msg, "content": base64.b64encode(text.encode()).decode(), "branch": "main",
            "committer": {"name": "HypeOtter bot", "email": "357183714+HypeOtter@users.noreply.github.com"}}
    if sha: body["sha"] = sha
    r = requests.put(f"https://api.github.com/repos/{PRIV}/contents/{path}", headers=_h(), json=body, timeout=30)
    r.raise_for_status()


def tg_api(method, **data):
    tok = os.environ["TELEGRAM_BOT_TOKEN"].strip().removeprefix("bot").strip()
    try: return requests.post(f"https://api.telegram.org/bot{tok}/{method}", data=data, timeout=30).json()
    except Exception: return {}


def now():
    return int(time.time())


def kb(i):
    return {"inline_keyboard": [
        [{"text": "🛠 Manage", "callback_data": f"dm:m:{i}"}, {"text": "↩️ Respond once", "callback_data": f"dm:r:{i}"}],
        [{"text": "✋ Hold", "callback_data": f"dm:h:{i}"}, {"text": "✍️ Custom", "callback_data": f"dm:c:{i}"}]]}


class Inbox:
    def __init__(self, tg, get, put, cfg):
        self.tg, self.get, self.put = tg, get, put
        dmc = cfg.get("dm", {}) if isinstance(cfg, dict) else {}
        self.enabled = dmc.get("enabled", True)
        self.hold_h = float(dmc.get("hold_hours", 12))
        self.replies = dmc.get("respond_once_lines") or REPLIES
        self.chat = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("native", {}).get("telegram_chat_id") or dmc.get("telegram_chat_id")
        raw, self.sha = get(STATE)
        self.st = json.loads(raw) if raw else {}
        self.orig = copy.deepcopy(self.st.get("items", {}))
        for k, v in (("next", 1), ("items", {}), ("seen", {}), ("seeded", {}), ("prompts", {}), ("last_lines", [])):
            self.st.setdefault(k, v)
        self.changed = False
        self.tok = os.environ.get("META_PAGE_TOKEN", "").strip()
        self.ig = os.environ.get("IG_USER_ID", "").strip()

    # ---------- which Telegram updates are ours ----------
    def owns(self, u):
        cq = u.get("callback_query")
        if cq: return (cq.get("data") or "").startswith("dm:")
        rep = ((u.get("message") or {}).get("reply_to_message") or {}).get("message_id")
        return rep is not None and (str(rep) in self.st["prompts"] or self._by_msg(rep) is not None)

    def _by_msg(self, mid):
        for i, it in self.st["items"].items():
            if mid in it.get("msgs", []): return i
        return None

    # ---------- sending replies ----------
    def send(self, it, text):
        if it["platform"] == "test":
            return True, "test mode, nothing was sent"
        if not self.tok:
            return False, "META_PAGE_TOKEN is not set"
        try:
            j = requests.post(f"{GRAPH}/me/messages", params={"access_token": self.tok}, timeout=30,
                              json={"recipient": {"id": it["user_id"]}, "messaging_type": "RESPONSE",
                                    "message": {"text": text}}).json()
        except Exception as e:
            return False, type(e).__name__
        if "error" in j:
            msg = j["error"].get("message", "error")
            if "outside of allowed window" in msg or j["error"].get("error_subcode") == 2018278:
                msg = "the 24 hour reply window has closed, answer it in the inbox"
            return False, msg[:200]
        return True, ""

    def _alert(self, it, prefix="📩"):
        flag = "\n⚠️ Money, deal or legal words. Your call." if FLAG.search(it["text"]) else ""
        txt = (f"{prefix} {LABEL.get(it['platform'], it['platform'])} DM from {it['user']}{flag}\n\n"
               f"{it['text'][:1500]}\n\nInbox: {INBOX.get(it['platform'], '')}")
        r = self.tg("sendMessage", chat_id=self.chat, text=txt, disable_web_page_preview="true",
                    reply_markup=json.dumps(kb(it["id"])))
        mid = (r.get("result") or {}).get("message_id")
        if mid: it.setdefault("msgs", []).append(mid)
        self.changed = True

    def new_item(self, d):
        i = self.st["next"]; self.st["next"] += 1
        it = dict(d, id=i, status="new", created=now(), msgs=[])
        self.st["items"][str(i)] = it
        self._alert(it)
        print("dm alert", it["platform"], i)

    # ---------- Meta polling ----------
    def poll_meta(self):
        if not self.tok: return []
        try:
            me = requests.get(f"{GRAPH}/me", params={"fields": "id", "access_token": self.tok}, timeout=30).json()
        except Exception:
            print("meta: request failed"); return []
        if "error" in me:
            print("meta: token error", me["error"].get("code")); return []
        own = {me.get("id"), self.ig}
        found = []
        for plat, p in (("facebook", "messenger"), ("instagram", "instagram")):
            if plat == "instagram" and not self.ig: continue
            try:
                r = requests.get(f"{GRAPH}/me/conversations", timeout=30, params={
                    "platform": p, "limit": 20, "access_token": self.tok,
                    "fields": "id,updated_time,messages.limit(5){id,message,from,created_time}"}).json()
            except Exception:
                print(plat, "request failed"); continue
            if "error" in r:
                print(plat, "error", r["error"].get("code"), r["error"].get("message", "")[:120]); continue
            seen = self.st["seen"].setdefault(plat, {})
            first = not self.st["seeded"].get(plat)
            for c in r.get("data", []):
                msgs = (c.get("messages") or {}).get("data", [])  # newest first
                new = []
                for m in msgs:
                    if m.get("id") == seen.get(c["id"]): break
                    new.append(m)
                if msgs and seen.get(c["id"]) != msgs[0].get("id"):
                    seen[c["id"]] = msgs[0]["id"]; self.changed = True
                inbound = []
                for m in reversed(new):
                    f = m.get("from") or {}
                    if f.get("id") in own: inbound = []; continue  # we already answered after these
                    try: ts = int(dt.datetime.strptime(m["created_time"], "%Y-%m-%dT%H:%M:%S%z").timestamp())
                    except Exception: ts = now()
                    if first and ts < now() - 86400: continue
                    inbound.append((f, m.get("message") or "(photo, video or sticker)"))
                if inbound:
                    f = inbound[-1][0]
                    found.append({"platform": plat, "conv": c["id"], "user_id": f.get("id"),
                                  "user": f.get("username") or f.get("name") or "someone",
                                  "text": "\n".join(t for _, t in inbound)})
            if first: self.st["seeded"][plat] = True; self.changed = True
        return found

    # ---------- button taps ----------
    def on_callback(self, cq):
        _, act, i = (cq.get("data") or "dm:x:0").split(":", 2)
        it = self.st["items"].get(i)
        chat = cq["message"]["chat"]["id"]; mid = cq["message"]["message_id"]
        if not it:
            self.tg("answerCallbackQuery", callback_query_id=cq["id"], text="That DM is too old."); return
        self.changed = True
        if act == "m":
            it["status"] = "managed"
            self.tg("answerCallbackQuery", callback_query_id=cq["id"], text="Yours. I'll stop nagging.")
            self.tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=json.dumps(
                {"inline_keyboard": [[{"text": "🛠 You're handling it. Open inbox", "url": INBOX.get(it["platform"], "https://facebook.com")}]]}))
        elif act == "r":
            if it.get("auto_sent"):
                self.tg("answerCallbackQuery", callback_query_id=cq["id"], text="Already answered once."); return
            pool = [x for x in self.replies if x not in self.st["last_lines"]] or self.replies
            line = random.choice(pool)
            ok, err = self.send(it, line)
            self.tg("answerCallbackQuery", callback_query_id=cq["id"], text="Sent" if ok else "Didn't send")
            if ok:
                it.update(status="replied", auto_sent=True)
                self.st["last_lines"] = (self.st["last_lines"] + [line])[-3:]
                self.tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=json.dumps({"inline_keyboard": []}))
                self.tg("sendMessage", chat_id=chat, reply_to_message_id=mid, text=f"↩️ Sent: {line}" + (f" ({err})" if err else ""))
            else:
                self.tg("sendMessage", chat_id=chat, reply_to_message_id=mid, text=f"❌ Didn't send: {err}")
            print("dm respond-once", i, ok)
        elif act == "h":
            it.update(status="hold", hold_until=now() + int(self.hold_h * 3600))
            self.tg("answerCallbackQuery", callback_query_id=cq["id"], text=f"Held. I'll remind you in {self.hold_h:g} h.")
            print("dm hold", i)
        elif act == "c":
            self.tg("answerCallbackQuery", callback_query_id=cq["id"])
            r = self.tg("sendMessage", chat_id=chat, reply_to_message_id=mid,
                        text=f"✍️ Type your reply to {it['user']} and send it. It goes out as the DM.",
                        reply_markup=json.dumps({"force_reply": True, "input_field_placeholder": "Your reply"}))
            pm = (r.get("result") or {}).get("message_id")
            if pm: self.st["prompts"][str(pm)] = i
            print("dm custom prompt", i)

    # ---------- typed replies ----------
    def on_message(self, msg):
        rep = (msg.get("reply_to_message") or {}).get("message_id")
        i = self.st["prompts"].get(str(rep)) or self._by_msg(rep)
        it = self.st["items"].get(str(i)) if i else None
        text = (msg.get("text") or "").strip()
        if not it or not text: return
        chat = msg["chat"]["id"]
        ok, err = self.send(it, text[:1000])
        self.changed = True
        if ok:
            it["status"] = "replied"; self.st["prompts"].pop(str(rep), None)
            for m in it.get("msgs", []):
                self.tg("editMessageReplyMarkup", chat_id=chat, message_id=m, reply_markup=json.dumps({"inline_keyboard": []}))
            self.tg("sendMessage", chat_id=chat, reply_to_message_id=msg["message_id"],
                    text=f"✅ Sent to {it['user']}" + (f" ({err})" if err else ""))
        else:
            self.tg("sendMessage", chat_id=chat, reply_to_message_id=msg["message_id"], text=f"❌ Didn't send: {err}")
        print("dm custom", i, ok)

    def save(self):
        if not self.changed: return
        try:
            self.put(STATE, json.dumps(self.st, indent=1), self.sha, "dm inbox state"); return
        except Exception:
            print("dm state conflict, merging")
        raw, sha = self.get(STATE)
        cur = json.loads(raw) if raw else {}
        mine = {k: v for k, v in self.st["items"].items() if self.orig.get(k) != v}
        cur.setdefault("items", {}).update(mine)
        cur["next"] = max(cur.get("next", 1), self.st["next"])
        cur.setdefault("prompts", {}).update(self.st["prompts"])
        for plat, d in self.st["seen"].items(): cur.setdefault("seen", {}).setdefault(plat, {}).update(d)
        cur.setdefault("seeded", {}).update(self.st["seeded"])
        cur["last_lines"] = self.st["last_lines"]
        self.put(STATE, json.dumps(cur, indent=1), sha, "dm inbox state (merged)")

    def hook(self, u):
        if self.enabled and self.chat and self.owns(u):
            self.run([u], poll=False)
        else:
            print("not a dm update")

    # ---------- one run ----------
    def run(self, updates, poll=True):
        if not self.enabled or not self.chat:
            print("dm inbox off" if self.enabled else "dm inbox: no telegram chat id"); return
        for u in updates:
            if u.get("callback_query"): self.on_callback(u["callback_query"])
            elif u.get("message"): self.on_message(u["message"])
        if poll and os.environ.get("DM_TEST") == "1":
            self.new_item({"platform": "test", "conv": "test", "user_id": "0", "user": "@test_fan",
                           "text": "yo Otty, that CaseOH clip had me dying. you making more?"})
        for d in (self.poll_meta() if poll else []):
            self.new_item(d)
        for it in self.st["items"].values():  # held DMs come back
            if poll and it.get("status") == "hold" and it.get("hold_until") and it["hold_until"] <= now():
                it.update(status="new", hold_until=None); self._alert(it, prefix="⏰ Reminder:")
        cut = now() - 14 * 86400
        for k in [k for k, it in self.st["items"].items() if it.get("created", 0) < cut]:
            self.st["items"].pop(k); self.changed = True
        live = {m for it in self.st["items"].values() for m in it.get("msgs", [])}
        self.st["prompts"] = {p: i for p, i in self.st["prompts"].items() if i in self.st["items"]}
        self.save()
        print("dm inbox done, open", sum(1 for it in self.st["items"].values() if it["status"] in ("new", "hold")), "msgs", len(live))


def main():
    raw, _ = gh_get("social/config.json")
    ib = Inbox(tg_api, gh_get, gh_put, json.loads(raw) if raw else {})
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        ib.hook(json.loads(os.environ.get("UPDATE") or "{}"))
    else:
        ib.run([])


if __name__ == "__main__":
    main()
