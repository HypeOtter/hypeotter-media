#!/usr/bin/env python3
"""Hourly: any review video Otty hasn't answered in 24 hours (no Approve/Hold, or Hold with no notes) gets one
"Needs attention" reply under its Telegram message, repeated at most once every 24 hours. Logs print counts only."""
import base64, json, os, time, requests
TOK = os.environ["TELEGRAM_BOT_TOKEN"].strip().removeprefix("bot").strip(); API = f"https://api.telegram.org/bot{TOK}"
PRIV = "HypeOtter/HypeOtter"
H = {"Authorization": f"Bearer {os.environ['PRIVATE_DATA_TOKEN']}", "Accept": "application/vnd.github+json"}
DAY = 24 * 3600

def get(path):
    r = requests.get(f"https://api.github.com/repos/{PRIV}/contents/{path}", headers=H, timeout=30); r.raise_for_status()
    j = r.json(); return base64.b64decode(j["content"]).decode(), j["sha"]

def main():
    raw, sha = get("review.json"); d = json.loads(raw)
    chat = get("chat_id.txt")[0].strip()
    now, n = time.time(), 0
    for s, it in d.get("items", {}).items():
        waiting = it.get("status") == "pending" or (it.get("status") == "hold" and not it.get("notes"))
        if not waiting or not it.get("msgs"):
            continue
        if now - it.get("sent_at", 0) < DAY or now - it.get("nudged_at", 0) < DAY:
            continue
        txt = "⚠️ Needs attention: Approve or Hold this one." if it["status"] == "pending" else \
              "⚠️ Needs attention: on Hold with no notes. Reply with notes, or kill."
        r = requests.post(f"{API}/sendMessage", timeout=30, data={"chat_id": chat, "text": txt,
                          "reply_to_message_id": it["msgs"][0], "allow_sending_without_reply": True}).json()
        if r.get("ok"):
            it["nudged_at"] = now; n += 1
            mid = r["result"]["message_id"]; it["msgs"].append(mid)  # replying "approve"/"hold"/notes here works too
    done = {s: (it["nudged_at"], it["msgs"]) for s, it in d.get("items", {}).items() if it.get("nudged_at") == now}
    for attempt in range(5):  # review.json changes often: re-read and merge until the save lands
        if not n: break
        if attempt:
            time.sleep(3); raw, sha = get("review.json"); d = json.loads(raw)
            for s, (t, msgs) in done.items():
                it = d["items"].get(s)
                if it: it["nudged_at"] = t; it["msgs"] = list(dict.fromkeys(it.get("msgs", []) + msgs))
        body = {"message": f"attention: {n} reminder(s)", "content": base64.b64encode(json.dumps(d, indent=1).encode()).decode(),
                "sha": sha, "branch": "main",
                "committer": {"name": "HypeOtter bot", "email": "357183714+HypeOtter@users.noreply.github.com"}}
        r = requests.put(f"https://api.github.com/repos/{PRIV}/contents/review.json", headers=H, json=body, timeout=30)
        if r.status_code != 409: r.raise_for_status(); break
    print("reminders sent:", n)

if __name__ == "__main__":
    main()
