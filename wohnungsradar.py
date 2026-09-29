#!/usr/bin/env python3
"""
Wohnungsradar Hamburg
Prüft die Websites der Hausverwaltungen aus config.yaml auf neue
Mietangebote, gleicht sie mit den Kriterien ab und meldet Treffer
per Telegram und/oder E-Mail.

Aufruf:
  python wohnungsradar.py              normaler Lauf
  python wohnungsradar.py --dry-run    nichts senden, nichts speichern
  python wohnungsradar.py --diagnose   Status aller Quellen ausgeben
"""
import hashlib
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

BASIS = Path(__file__).parent
CONFIG = BASIS / "config.yaml"
STATE = BASIS / "state.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36 Wohnungsradar/1.0 (private Wohnungssuche)")
PAUSE = 1.5                 # Sekunden zwischen Abrufen (höflich bleiben)
MAX_DETAILS_PRO_QUELLE = 10
MAX_TREFFER_PRO_MAIL = 15
MEHRDEUTIG = {"altstadt", "neustadt", "eppendorf"}
WARNUNG_NACH_FEHLLAEUFEN = 36   # ~12 Std. bei 20-Minuten-Takt

DRY = "--dry-run" in sys.argv
DIAG = "--diagnose" in sys.argv

# ------------------------------------------------------------------ Abruf
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})


def hole(url):
    r = SESSION.get(url, timeout=25)
    r.raise_for_status()
    if r.encoding is None or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    time.sleep(PAUSE)
    return r.text


def suppe(html):
    s = BeautifulSoup(html, "html.parser")
    for t in s(["script", "style", "noscript", "svg"]):
        t.decompose()
    return s


def seitentext(s):
    for t in s(["nav", "header", "footer"]):
        t.decompose()
    txt = s.get_text("\n")
    zeilen = [re.sub(r"\s+", " ", z).strip() for z in txt.splitlines()]
    return "\n".join(z for z in zeilen if z)


def titel(s):
    h = s.find("h1")
    if h and h.get_text(strip=True):
        return h.get_text(" ", strip=True)[:120]
    return (s.title.get_text(strip=True) if s.title else "")[:120]


# ------------------------------------------------------- Links finden
ANGEBOT_HINWEIS = re.compile(
    r"expos|objekt|immobilie|angebot|wohnung|detail|estate|property|miet|"
    r"vermiet|/id[/=]|\d{4,}", re.I)
KEIN_ANGEBOT = re.compile(
    r"impressum|datenschutz|kontakt|karriere|jobs|agb|widerruf|login|portal\.|"
    r"facebook|instagram|linkedin|xing|youtube|twitter|whatsapp|google|"
    r"mailto:|tel:|javascript:|\.(jpg|jpeg|png|gif|webp|svg|zip)$|#$|"
    r"cookie|sitemap|newsletter|blog|news|team|ueber-uns|über-uns|"
    r"verkauf|kaufen|eigentumswohnung|gewerbe|buero|büro|bewertung|tippgeber",
    re.I)
PORTALE = ("immobilienscout24", "immowelt", "immonet", "immomio",
           "ivd24", "immobilie1", "ohne-makler", "kleinanzeigen")
NAVI_HINWEIS = re.compile(r"miet|vermietung|wohnungsangebot|angebote|"
                          r"freie wohnungen|wohnungen|objekte|immobilien", re.I)


def domain(u):
    h = urlparse(u).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def angebotslinks(s, basis_url, muster=None):
    """Alle Links, die nach einzelnen Angeboten aussehen."""
    eigene = domain(basis_url)
    rx = re.compile(muster, re.I) if muster else None
    gefunden = {}
    for a in s.find_all("a", href=True):
        href = a["href"].strip()
        voll = urljoin(basis_url, href).split("#")[0]
        if not voll.startswith("http") or voll.rstrip("/") == basis_url.rstrip("/"):
            continue
        text = a.get_text(" ", strip=True)
        d = domain(voll)
        if rx:
            if rx.search(voll):
                gefunden[voll] = text
            continue
        if d != eigene and not any(p in d for p in PORTALE):
            continue
        if KEIN_ANGEBOT.search(voll) or KEIN_ANGEBOT.search(text or ""):
            continue
        if ANGEBOT_HINWEIS.search(voll) or ANGEBOT_HINWEIS.search(text or ""):
            gefunden[voll] = text
    return gefunden


def finde_angebotsseiten(start_url):
    """Von der Startseite aus Unterseiten wie 'Vermietung' finden."""
    s = suppe(hole(start_url))
    kandidaten = []
    for a in s.find_all("a", href=True):
        voll = urljoin(start_url, a["href"]).split("#")[0]
        text = a.get_text(" ", strip=True)
        if domain(voll) != domain(start_url):
            continue
        if re.search(r"verkauf|kauf|eigentum|gewerbe|büro|referenz|verkauft|vermietet$",
                     text + " " + voll, re.I):
            continue
        if NAVI_HINWEIS.search(text) or re.search(r"miet|vermiet", voll, re.I):
            if voll not in kandidaten and voll.rstrip("/") != start_url.rstrip("/"):
                kandidaten.append(voll)
    # Seiten mit "miet" im Namen zuerst
    kandidaten.sort(key=lambda u: 0 if re.search(r"miet", u, re.I) else 1)
    return kandidaten[:3] or [start_url]


# ------------------------------------------------- Kriterien prüfen
def zahl(txt):
    txt = txt.strip().replace(" ", "")
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", txt):
        txt = txt.replace(".", "")
    try:
        return float(txt)
    except ValueError:
        return None


NUM = r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)"
VERNEINT = re.compile(r"(kein|keine|keinen|ohne|nicht|kein\(e\))\s*\S*\s*$", re.I)


def gefunden(rx, text):
    """Treffer, der nicht durch 'kein/ohne/nicht' davor verneint ist.
    Rückgabe: 'ja', 'nein' (verneint) oder None (nicht erwähnt)."""
    ergebnis = None
    for m in re.finditer(rx, text, re.I):
        davor = text[max(0, m.start() - 25):m.start()]
        if VERNEINT.search(davor):
            ergebnis = ergebnis or "nein"
        else:
            return "ja"
    return ergebnis


def ist_mietangebot(text):
    t = text.lower()
    hat_groesse = re.search(r"zimmer|m²|m2\b|qm\b|wohnfläche", t)
    hat_miete = re.search(r"miete|mietpreis|zu vermieten|vermietung", t)
    nur_kauf = "kaufpreis" in t and not re.search(r"kaltmiete|warmmiete|miete zzgl|mietpreis", t)
    return bool(hat_groesse and hat_miete and not nur_kauf)


def bewerte(text, k):
    t = text.lower()
    info, ok, fehlt, grund, stern = {}, [], [], [], []

    # Kaltmiete
    kalt = None
    for rx in [r"(?:netto)?kaltmiete[^\d\n]{0,30}" + NUM,
               r"grundmiete[^\d\n]{0,30}" + NUM,
               r"miete\s*(?:zzgl\.?|zuzüglich)\s*(?:nk|nebenkosten|bk)?[^\d\n]{0,15}" + NUM,
               NUM + r"\s*€?\s*(?:eur)?\s*(?:kaltmiete|kalt\b|miete zzgl|zzgl\.?\s*nk)"]:
        m = re.search(rx, t)
        if m:
            kalt = zahl(m.group(1))
            if kalt and 150 <= kalt <= 20000:
                break
            kalt = None
    warm = None
    m = re.search(r"warmmiete[^\d\n]{0,30}" + NUM, t)
    if m:
        warm = zahl(m.group(1))
    if kalt:
        info["Kaltmiete"] = f"{kalt:,.0f} €".replace(",", ".")
        if kalt > k["max_kaltmiete"]:
            grund.append("zu teuer")
    elif warm:
        info["Warmmiete"] = f"{warm:,.0f} €".replace(",", ".")
        if warm > k["max_kaltmiete"]:
            fehlt.append("Kaltmiete unklar")

    # Zimmer
    zi = None
    for rx in [r"(\d(?:[.,]5)?)\s*-?\s*zi(?:mmer|\.|\b)",
               r"zimmer(?:anzahl)?[:\s]+(\d(?:[.,]5)?)"]:
        m = re.search(rx, t)
        if m:
            zi = zahl(m.group(1))
            break
    if zi:
        info["Zimmer"] = f"{zi:g}".replace(".", ",")
        if zi < k["min_zimmer"]:
            grund.append("zu wenig Zimmer")

    # Fläche
    fl = None
    for rx in [r"wohnfläche[^\d\n]{0,20}(\d{2,3}(?:[.,]\d{1,2})?)",
               r"(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:m²|m2\b|qm\b|quadratmeter)"]:
        m = re.search(rx, t)
        if m:
            fl = zahl(m.group(1))
            if fl and 10 <= fl <= 500:
                break
            fl = None
    if fl:
        info["Fläche"] = f"{fl:g} m²".replace(".", ",")
        if fl < k["min_flaeche"]:
            grund.append("zu klein")

    # Lage – streng: nur Wunsch-Stadtteile in Hamburg
    # Maßgeblich ist die ERSTE Postleitzahl im Text (die der Wohnung;
    # Büro-Adressen der Verwaltung stehen meist weiter unten).
    lage = [s for s in k["stadtteile"]
            if re.search(r"\b" + re.escape(s.lower()) + r"\b", t)]
    plz_liste = [int(p) for p in re.findall(r"\b(\d{5})\s*,?\s*[a-zäöü]{3,}", t)]
    erlaubt = set(k["postleitzahlen"])
    if plz_liste:
        erste = plz_liste[0]
        if erste in erlaubt:
            info["Lage"] = ", ".join(lage) if lage else f"PLZ {erste}"
        elif 20000 <= erste <= 22999:
            grund.append(f"Hamburg, aber anderer Stadtteil (PLZ {erste})")
        else:
            grund.append(f"nicht Hamburg (PLZ {erste})")
    elif lage and ("hamburg" in t or any(x.lower() not in MEHRDEUTIG for x in lage)):
        # Altstadt/Neustadt/Eppendorf gibt es auch anderswo -> dann muss "Hamburg" dabeistehen
        info["Lage"] = ", ".join(lage)
    else:
        grund.append("Lage nicht erkennbar / nicht Hamburg")

    # No-Gos
    a = k["ausschluss"]
    if a.get("moebliert"):
        if gefunden(r"(?<!un)(?<!teil)möbliert|moebliert|furnished|wohnen auf zeit", t) == "ja":
            grund.append("möbliert")
        elif "teilmöbliert" in t:
            fehlt.append("teilmöbliert")
    if a.get("befristet"):
        if (gefunden(r"(?<!un)befristet|zeitmietvertrag|zwischenmiete|untermiete", t) == "ja"):
            grund.append("befristet")
        elif "unbefristet" in t:
            ok.append("unbefristet")
    if a.get("nachtspeicher") and gefunden(r"nachtspeicher|nachtstromspeicher", t) == "ja":
        grund.append("Nachtspeicherheizung")
    if a.get("dachgeschoss") and gefunden(
            r"dachgeschoss|dachwohnung|\bdg\b|dachschräge", t) == "ja":
        grund.append("Dachgeschoss")

    # Muss
    mu = k["muss"]
    if mu.get("einbaukueche"):
        r = gefunden(r"einbauküche|\bebk\b|küchenzeile|einbaukueche|inkl\.? küche|"
                     r"mit küche|küche vorhanden|ausgestattete küche", t)
        if r == "ja":
            ok.append("EBK")
        elif r == "nein":
            grund.append("keine EBK")
        else:
            fehlt.append("EBK")
    if mu.get("balkon"):
        if "französischer balkon" in t and not re.search(r"(?<!französischer )balkon", t):
            fehlt.append("nur franz. Balkon")
        else:
            r = gefunden(r"balkon|loggia", t)
            if r == "ja":
                ok.append("Balkon")
            elif r == "nein":
                grund.append("kein Balkon")
            elif gefunden(r"terrasse", t) == "ja":
                ok.append("Terrasse")
            else:
                fehlt.append("Balkon")

    # Wünsche
    w = k["wuensche"]
    if w.get("altbau") and re.search(
            r"altbau|gründerzeit|jugendstil|stuck|baujahr[^\d]{0,10}(18\d\d|19[0-3]\d)", t):
        stern.append("Altbau")
    if w.get("dielen") and re.search(r"dielen|schiffsboden", t):
        stern.append("Dielen")
    m = re.search(r"(?:frei ab|bezugsfrei(?: ab)?|verfügbar ab|bezugstermin|einzug ab|"
                  r"verfügbarkeit|bezug ab)[:\s]*(\d{1,2}\.\s?\d{1,2}\.(?:\s?\d{2,4})?|"
                  r"sofort|nach vereinbarung|[a-zä]+\s\d{4})", t)
    if m and m.group(1).strip():
        wert = m.group(1).strip(" :.")
        info["Frei ab"] = wert
        if any(f in wert for f in w.get("frei_ab", [])):
            stern.append("Einzug passt")

    return {"info": info, "ok": ok, "fehlt": fehlt, "grund": grund, "stern": stern}


# ------------------------------------------------------- Meldungen
def meldung_text(quelle, titel_, url, b, html=True):
    e = escape if html else (lambda x: x)
    kopf = "🟢 Treffer" if not b["fehlt"] else "🟡 Möglicher Treffer – bitte prüfen"
    zeilen = [f"<b>{kopf}</b> · {e(quelle)}" if html else f"{kopf} · {quelle}"]
    zeilen.append(f'<a href="{e(url)}">{e(titel_ or "Zum Angebot")}</a>' if html
                  else f"{titel_}\n{url}")
    if b["info"]:
        zeilen.append(" · ".join(f"{k}: {e(v)}" for k, v in b["info"].items()))
    teile = [f"✅ {x}" for x in b["ok"]] + [f"❓ {x}" for x in b["fehlt"]]
    if teile:
        zeilen.append("  ".join(teile))
    if b["stern"]:
        zeilen.append("⭐ " + ", ".join(b["stern"]))
    return "\n".join(zeilen)


def sende(text_html, text_plain, betreff="Wohnungsradar"):
    if DRY or DIAG:
        print("\n--- MELDUNG ---\n" + text_plain + "\n")
        return
    tok, chats = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID", "")
    if tok and chats:
        stuecke, akt = [], ""
        for block in text_html.split("\n\n"):
            if len(akt) + len(block) > 3800:
                stuecke.append(akt); akt = ""
            akt += ("\n\n" if akt else "") + block
        stuecke.append(akt)
        for cid in [c.strip() for c in chats.split(",") if c.strip()]:
            for teil in stuecke:
                try:
                    requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                                  data={"chat_id": cid, "text": teil, "parse_mode": "HTML",
                                        "disable_web_page_preview": "true"},
                                  timeout=20).raise_for_status()
                except Exception as ex:
                    print(f"Telegram-Fehler: {ex}")
    host, to = os.getenv("SMTP_HOST"), os.getenv("MAIL_TO")
    if host and to:
        try:
            msg = MIMEText(text_plain, "plain", "utf-8")
            msg["Subject"], msg["From"], msg["To"] = betreff, os.getenv("SMTP_USER"), to
            with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT", "465")), timeout=30) as s:
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
                s.sendmail(msg["From"], [x.strip() for x in to.split(",")], msg.as_string())
        except Exception as ex:
            print(f"E-Mail-Fehler: {ex}")


# ------------------------------------------------------- Hauptlauf
def lade_state():
    if STATE.exists():
        return json.loads(STATE.read_text("utf-8"))
    return {}


def h(x):
    return hashlib.sha1(x.encode("utf-8")).hexdigest()[:16]


def pruefe_quelle(q, st, k):
    """Gibt (Anzahl erkannter Einträge, Liste neuer Treffer) zurück."""
    seiten = finde_angebotsseiten(q["url"]) if q.get("auto") else [q["url"]]
    erst = "links" not in st
    links_alt, zeilen_alt = set(st.get("links", [])), set(st.get("zeilen", []))
    links_neu, zeilen_neu, treffer = {}, [], []

    for seite in seiten:
        s = suppe(hole(seite))
        links_neu.update(angebotslinks(s, seite, q.get("link_muster")))
        for z in seitentext(s).splitlines():
            zeilen_neu.append((seite, z))

    # 1) Neue Einzel-Angebote (Links) prüfen
    neue = [u for u in links_neu if u not in links_alt]
    if not erst:
        for u in neue[:MAX_DETAILS_PRO_QUELLE]:
            try:
                ds = suppe(hole(u))
            except Exception as ex:
                print(f"  Detailseite nicht erreichbar: {u} ({ex})")
                continue
            tit = titel(ds)
            txt = tit + "\n" + seitentext(ds)
            if ist_mietangebot(txt):
                treffer.append((tit, u, bewerte(txt, k)))

    # 2) Rückfall: neue Textzeilen auf der Übersichtsseite (für Seiten ohne Detail-Links)
    if not erst and not links_neu:
        neu_txt = [(s_, z) for s_, z in zeilen_neu if h(z) not in zeilen_alt]
        block = "\n".join(z for _, z in neu_txt)
        if neu_txt and ist_mietangebot(block):
            treffer.append((f"Neuer Eintrag auf {q['name']}", neu_txt[0][0], bewerte(block, k)))

    st["links"] = list((links_alt | set(links_neu)))[-3000:]
    st["zeilen"] = list(zeilen_alt | {h(z) for _, z in zeilen_neu})[-5000:]
    st["seiten"] = seiten
    return len(links_neu), treffer, erst


def main():
    cfg = yaml.safe_load(CONFIG.read_text("utf-8"))
    k = cfg["kriterien"]
    state = lade_state()
    quellen = state.setdefault("quellen", {})
    status = []
    sammel = []                                   # alle Treffer dieses Laufs
    gemeldet = set(state.get("gemeldet", []))     # nie doppelt melden

    for q in cfg["quellen"]:
        if q.get("aktiv") is False or not q.get("url"):
            continue
        st = quellen.setdefault(q["name"], {})
        try:
            anzahl, treffer, _ = pruefe_quelle(q, st, k)
            st["fehler"] = 0
            st["leer"] = 0 if anzahl else st.get("leer", 0) + 1
            status.append((q["name"], "ok", anzahl))
            print(f"✔ {q['name']}: {anzahl} Angebots-Links erkannt, {len(treffer)} neu")
            for tit, url, b in treffer:
                if b["grund"]:
                    print(f"   ✗ aussortiert ({', '.join(b['grund'])}): {url}")
                    continue
                if url in gemeldet:
                    continue
                gemeldet.add(url)
                sammel.append((q["name"], tit, url, b))
        except Exception as ex:
            st["fehler"] = st.get("fehler", 0) + 1
            status.append((q["name"], "fehler", str(ex)[:80]))
            print(f"⚠ {q['name']}: {ex}")
            if st["fehler"] == WARNUNG_NACH_FEHLLAEUFEN:
                sende(f"⚠️ <b>{escape(q['name'])}</b> ist seit längerem nicht erreichbar.",
                      f"⚠️ {q['name']} ist seit längerem nicht erreichbar.",
                      "Wohnungsradar: Quelle nicht erreichbar")

    # Alle Treffer dieses Laufs in EINER Nachricht
    if sammel:
        sammel.sort(key=lambda x: bool(x[3]["fehlt"]))   # 🟢 zuerst
        sammel = sammel[:MAX_TREFFER_PRO_MAIL]
        n = len(sammel)
        kopf = f"🏠 {n} neue passende Wohnung{'en' if n > 1 else ''}"
        teile_html = [meldung_text(*x, html=True) for x in sammel]
        teile_txt = [meldung_text(*x, html=False) for x in sammel]
        sende(f"<b>{kopf}</b>\n\n" + "\n\n".join(teile_html),
              kopf + "\n\n" + "\n\n----------\n\n".join(teile_txt),
              f"Wohnungsradar: {kopf[2:]}")
    state["gemeldet"] = list(gemeldet)[-3000:]

    # Beim allerersten Lauf: Überblick schicken, welche Quellen funktionieren
    if not state.get("gestartet") or DIAG:
        zeilen = ["📡 <b>Wohnungsradar ist aktiv.</b> Status der Quellen:"]
        for name, art, wert in status:
            if art == "fehler":
                zeilen.append(f"⚠️ {escape(name)}: nicht erreichbar")
            elif wert:
                zeilen.append(f"✅ {escape(name)}: {wert} Einträge erkannt")
            else:
                zeilen.append(f"○ {escape(name)}: erreichbar, derzeit keine Angebote erkannt")
        zeilen.append("Ab jetzt kommen nur noch neue, passende Wohnungen.")
        txt = "\n".join(zeilen)
        sende(txt, re.sub(r"</?b>", "", txt), "Wohnungsradar: gestartet")

    if not DRY and not DIAG:
        state["gestartet"] = True
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
