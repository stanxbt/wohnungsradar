# Wohnungsradar Hamburg – Einrichtung (ca. 15 Minuten)

Das Radar prüft alle 20 Minuten (ca. 7–23 Uhr) die Websites der Hausverwaltungen,
erkennt neue Mietangebote, gleicht sie mit den Kriterien ab und schickt passende
Wohnungen per Telegram (und/oder E-Mail). Es läuft kostenlos bei GitHub – kein
eigener Computer muss laufen.

## 1. Telegram-Bot anlegen (2 Minuten)

1. In Telegram den Kontakt **@BotFather** öffnen, `/newbot` senden, Namen vergeben.
2. BotFather antwortet mit einem **Token** (sieht aus wie `123456:ABC-...`). Notieren.
3. Den neuen Bot in Telegram öffnen und **Start** drücken (sonst darf er nicht schreiben).
4. Im Browser öffnen: `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Dort steht `"chat":{"id": 123456789 ...}` – diese Zahl ist die **Chat-ID**.

Mehrere Empfänger: jede Person drückt beim Bot auf Start, dann die Chat-IDs
mit Komma trennen (`111111,222222`).

## 2. GitHub-Repository anlegen

1. Kostenlosen Account auf github.com erstellen.
2. **New repository** → Name z. B. `wohnungsradar` → **Public** wählen
   (bei Public sind die Laufzeit-Minuten unbegrenzt kostenlos; im Repository
   stehen nur Kriterien und Links, keine persönlichen Daten – Token und Chat-ID
   liegen verschlüsselt in den Secrets).
   Bei **Private** im Workflow `*/20` auf `*/30` ändern, sonst reicht das
   Gratis-Kontingent nicht.
3. **Add file → Upload files** und alle Dateien aus diesem Ordner hochladen,
   inklusive des Ordners `.github/workflows/radar.yml`.
   (Tipp: Der Ordner `.github` ist evtl. versteckt. Alternativ in GitHub
   **Add file → Create new file**, als Namen `.github/workflows/radar.yml`
   eintippen und den Inhalt hineinkopieren.)

## 3. Zugangsdaten hinterlegen

Im Repository: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Wert |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token vom BotFather |
| `TELEGRAM_CHAT_ID` | Chat-ID(s) |

Optional zusätzlich E-Mail: `SMTP_HOST` (z. B. `smtp.gmail.com`), `SMTP_PORT` (`465`),
`SMTP_USER`, `SMTP_PASS` (bei Gmail ein **App-Passwort**), `MAIL_TO`.

## 4. Starten

**Actions** → Workflow **Wohnungsradar** → **Run workflow**.

Nach 1–3 Minuten kommt eine Telegram-Nachricht mit dem Status aller Quellen:
- ✅ Einträge erkannt → funktioniert
- ○ erreichbar, derzeit keine Angebote → funktioniert vermutlich, Seite ist gerade leer
- ⚠️ nicht erreichbar → Seite blockiert oder Adresse falsch

Der erste Lauf merkt sich nur die aktuellen Angebote. Ab dem zweiten Lauf kommen
ausschließlich **neue** Wohnungen.

## So sehen Meldungen aus

- 🟢 **Treffer** – alle Muss-Kriterien im Text bestätigt
- 🟡 **Möglicher Treffer** – nichts spricht dagegen, aber z. B. Balkon oder
  Einbauküche werden nicht erwähnt (❓). Lieber einmal zu viel melden als eine
  gute Wohnung verpassen.
- Aussortiert (keine Meldung): zu teuer, zu klein, < 2 Zimmer, falscher Stadtteil,
  möbliert, befristet, Nachtspeicher, Dachgeschoss, ausdrücklich „kein Balkon“
  oder „keine EBK“.
- ⭐ markiert Wünsche: Altbau, Dielen, Einzug 01.12./01.01.

## Anpassen

Alles steht in `config.yaml` (in GitHub auf die Datei klicken → Stift-Symbol):
Kriterien ändern, Quellen ergänzen (`name` + `url`), einzelne Quellen mit
`aktiv: false` pausieren. Für Portale wie ohne-makler.net kann man dort eine
Suche mit Filtern ausführen und die Ergebnis-URL als Quelle eintragen.

## Grenzen – bitte kennen

- Die Kriterien werden per Texterkennung geprüft. Das ist zuverlässig bei klaren
  Angaben („Kaltmiete 1.100 €“), aber nicht perfekt. Im Zweifel meldet das Radar.
- Seiten, die Angebote erst per JavaScript nachladen, liefern evtl. nichts
  (erkennbar am ○ im Statusbericht).
- ImmoScout24 blockiert automatische Abrufe. Hinsch & Völckers (inkl. Haueisen)
  inserieren nur dort → dafür direkt bei ImmoScout24 einen Suchauftrag mit
  denselben Kriterien und Push-Benachrichtigung anlegen. Das deckt nebenbei auch
  viele andere Verwaltungen der Liste ab.
- Ist eine Quelle ca. 12 Stunden am Stück nicht erreichbar, kommt eine Warnung.
- GitHub pausiert zeitgesteuerte Abläufe, wenn ein Repository 60 Tage keine
  Aktivität hat. Falls keine Meldungen mehr kommen: unter **Actions** den
  Workflow wieder aktivieren.
