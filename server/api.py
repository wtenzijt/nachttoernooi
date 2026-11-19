"""Aanmeld-API voor het nachttoernooi. Alleen stdlib: draait op nimbus2000 achter Caddy.

GET  /api/aanmelding  -> {"ok": true, "vrij": N}
POST /api/aanmelding  -> {"ok": true} of {"ok": false, "error": "..."}
python3 api.py --selftest  draait de regelcontrole tegen een tijdelijke database.
"""
import json, os, re, sqlite3, sys, threading, urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get("NT_DB", "/var/lib/nachttoernooi/aanmeldingen.db")
PORT = int(os.environ.get("NT_PORT", "8090"))
# Apps Script blijft alleen voor de notificatiemail en een back-up in de Sheet.
MAIL_HOOK = os.environ.get("NT_MAIL_HOOK", "")
DEADLINE = datetime(2027, 4, 2, tzinfo=timezone(timedelta(hours=2)))
MAX_DUOS = 50
CATEGORIEEN = {"Heren competitie", "Heren recreant", "Dames competitie",
               "Dames recreant", "Mix competitie", "Mix recreant"}
ORIGINS = {"https://wtenzijt.github.io", "https://nimbus2000.tail9c81a0.ts.net:8443",
           "https://nachttoernooi.bcassen.nl"}
VELDEN = ["speler1_naam", "speler1_email", "speler1_telefoon",
          "speler2_naam", "speler2_email", "categorie", "opmerking"]
lock = threading.Lock()


def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS aanmelding (
        id INTEGER PRIMARY KEY, tijd TEXT NOT NULL,
        speler1_naam TEXT, speler1_email TEXT, speler1_telefoon TEXT,
        speler2_naam TEXT, speler2_email TEXT, categorie TEXT, opmerking TEXT)""")
    return con


def vrij(con):
    return max(0, MAX_DUOS - con.execute("SELECT COUNT(*) FROM aanmelding").fetchone()[0])


def controleer(con, d, nu=None):
    """Geeft een foutmelding terug, of None als de aanmelding mag."""
    if (nu or datetime.now(timezone.utc)) >= DEADLINE:
        return "de inschrijving is gesloten"
    if any(not str(d.get(k, "")).strip() for k in VELDEN[:6]):
        return "er ontbreken velden"
    if any(len(str(d.get(k, ""))) > 300 for k in VELDEN):
        return "een veld is te lang"
    if d["categorie"] not in CATEGORIEEN:
        return "onbekende categorie"
    mail = lambda s: str(s).strip().lower()
    tel = lambda s: re.sub(r"\D", "", str(s))
    e1, e2, t1 = mail(d["speler1_email"]), mail(d["speler2_email"]), tel(d["speler1_telefoon"])
    if "@" not in e1 or "@" not in e2:
        return "ongeldig e-mailadres"
    if e1 == e2:
        return "speler 1 en speler 2 hebben hetzelfde e-mailadres"
    if vrij(con) == 0:
        return "het toernooi is vol"
    rijen = con.execute("SELECT lower(trim(speler1_email)), lower(trim(speler2_email)), "
                        "speler1_telefoon, categorie FROM aanmelding").fetchall()
    if any({r[0], r[1]} == {e1, e2} for r in rijen):
        return "dit duo staat al ingeschreven"
    met = [r for r in rijen if e1 in r[:2] or e2 in r[:2] or (t1 and tel(r[2]) == t1)]
    if len(met) >= 2:
        return "één van de spelers staat al 2x ingeschreven"
    if any(r[3] == d["categorie"] for r in met):
        return "één van de spelers staat al in deze categorie"
    return None


def bewaar(con, d):
    con.execute(f"INSERT INTO aanmelding (tijd, {', '.join(VELDEN)}) VALUES (?{', ?' * len(VELDEN)})",
                [datetime.now(timezone.utc).isoformat()] + [str(d.get(k, "")).strip() for k in VELDEN])
    con.commit()


def meld(d):
    if not MAIL_HOOK:
        return
    try:  # best effort: de aanmelding is al opgeslagen, mail mag mislukken
        urllib.request.urlopen(urllib.request.Request(
            MAIL_HOOK, json.dumps(d).encode(), {"Content-Type": "text/plain"}), timeout=20).read()
    except Exception as e:
        print("mailhook mislukt:", e, file=sys.stderr)


class H(BaseHTTPRequestHandler):
    def stuur(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        origin = self.headers.get("Origin", "")
        if origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] != "/api/aanmelding":
            return self.stuur({"ok": False}, 404)
        con = db()
        try:
            n = vrij(con)
        finally:
            con.close()
        self.stuur({"ok": True, "vrij": n})

    def do_POST(self):
        if self.path.split("?")[0] != "/api/aanmelding":
            return self.stuur({"ok": False}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10_000:
            return self.stuur({"ok": False, "error": "te groot"}, 413)
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
            assert isinstance(d, dict)
        except Exception:
            return self.stuur({"ok": False, "error": "ongeldige aanvraag"}, 400)
        with lock:
            con = db()
            try:
                fout = controleer(con, d)
                if not fout:
                    bewaar(con, d)
            finally:
                con.close()
        if fout:
            return self.stuur({"ok": False, "error": fout})
        threading.Thread(target=meld, args=(d,), daemon=True).start()
        self.stuur({"ok": True})

    def log_message(self, *a):  # geen persoonsgegevens in de log
        pass


def selftest():
    global DB
    import tempfile
    DB = os.path.join(tempfile.mkdtemp(), "t.db")
    con = db()
    voor = datetime(2027, 1, 1, tzinfo=timezone.utc)
    duo = lambda a, b, cat, t="0612345678": {"speler1_naam": a, "speler1_email": f"{a}@x.nl",
                                             "speler1_telefoon": t, "speler2_naam": b,
                                             "speler2_email": f"{b}@x.nl", "categorie": cat}
    ok = lambda d: controleer(con, d, voor) or bewaar(con, d)
    assert ok(duo("william", "johan", "Heren competitie")) is None
    assert controleer(con, duo("william", "johan", "Mix competitie"), voor) == "dit duo staat al ingeschreven"
    assert controleer(con, duo("william", "anna", "Heren competitie"), voor) == "één van de spelers staat al in deze categorie"
    assert ok(duo("william", "anna", "Mix competitie", "0699999999")) is None
    assert controleer(con, duo("william", "piet", "Heren recreant", "0688888888"), voor) == "één van de spelers staat al 2x ingeschreven"
    assert controleer(con, duo("a", "a", "Mix recreant"), voor) == "speler 1 en speler 2 hebben hetzelfde e-mailadres"
    assert controleer(con, {**duo("x", "y", "Mix recreant"), "categorie": "Enkel"}, voor) == "onbekende categorie"
    assert controleer(con, duo("x", "y", "Mix recreant"), DEADLINE) == "de inschrijving is gesloten"
    for i in range(48):
        bewaar(con, duo(f"p{i}", f"q{i}", "Mix recreant", str(i)))
    assert vrij(con) == 0 and controleer(con, duo("x", "y", "Mix recreant", "1"), voor) == "het toernooi is vol"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        db().close()
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
