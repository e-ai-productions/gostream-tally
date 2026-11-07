#!/usr/bin/env python3
"""
GoStream Tally — preview/on-air viewer voor OSEE GoStream (Duet 8 ISO, Deck, Duet, Omni)

WAAROM DIT BESTAAT
Een telefoon kan zelf geen TCP-Verbinding met de switcher openen. Daarom draait dit
script op een computer in hetzelfde netwerk als de switcher. Het script:
  1. verbindt met de switcher via het GoStream Protocol (GSP) op TCP-poort 19010,
  2. leest continu pgmIndex (on air) en pvwIndex (preview), plus keyOnAir, dskOnAir, ftb,
  3. serveert een telefoonvriendelijke pagina op http://<dit-ip>:8080

PROTOCOL (GSP), zoals gedocumenteerd in de open-source Companion-module
(bitfocus/companion-module-osee-gostream-series):
  packet = 0xEB 0xA6 | protoid 0x00 | U16LE(len+2) | JSON | U16LE(CRC16-modbus van alles ervoor)
  commando = {"id": "<param>", "type": "get"|"set"|"pus", "value": [...]}
  push ("pus") wordt door de switcher gestuurd zodra er iets verandert.

GEBRUIK
  python3 gostream_tally.py --host 192.168.1.50
  python3 gostream_tally.py --host 192.168.1.50 --labels "Cam 1,Cam 2,Cam 3,Cam 4,Cam 5,Cam 6,Cam 7,Cam 8"
  python3 gostream_tally.py --demo          # testen zonder switcher

Open daarna op de telefoon: http://<ip-van-deze-computer>:8080
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import json
import queue
import random
import secrets
import urllib.parse
import webbrowser
import socket
import sys
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEAD1, HEAD2, PROTO = 0xEB, 0xA6, 0x00
SWITCHER_PORT = 19010

# Parameters die we volgen. pgmIndex = on air, pvwIndex = preview.
# Bronnummers uit het GoStream-protocol (GSP). De switcher stuurt alleen
# nummers, geen namen, dus de weergavenamen staan hier en zijn aanpasbaar.
VERSION = "1.6.1"

# De GoStream Duet 8 meldt de vijfde tot en met de achtste ingang als
# AUX 1 t/m AUX 4 (bronnummers 4001-4004). Voor de bediening zijn dat gewoon
# camera 5 tot en met 8, dus die nummers worden omgezet.
SOURCE_ALIASES = {4001: 5, 4002: 6, 4003: 7, 4004: 8}


def parse_alias(tekst: str) -> dict[int, int]:
    """Leest '4001=5,4002=6' als omzettingstabel."""
    uit: dict[int, int] = {}
    for deel in (tekst or "").split(","):
        if "=" not in deel:
            continue
        links, rechts = deel.split("=", 1)
        try:
            uit[int(links.strip())] = int(rechts.strip())
        except ValueError:
            continue
    return uit

DEFAULT_NAMES = {
    0: "Zwart",
    1: "Cam 1", 2: "Cam 2", 3: "Cam 3", 4: "Cam 4",
    5: "Cam 5", 6: "Cam 6", 7: "Cam 7", 8: "Cam 8",
    1000: "Kleurbalken", 2001: "Kleur", 2002: "Kleur 2", 9001: "Multiview",
    3010: "Media", 3011: "Media key", 3020: "Media 2", 3021: "Media 2 key",
    5001: "MultiSource", 10010: "PGM", 10011: "PVW",
}


def data_dir() -> str:
    """De map waarin labels.json hoort: naast het script of naast de .exe."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def names_path() -> str:
    return os.path.join(data_dir(), "labels.json")


def switcher_path() -> str:
    """Onthoudt het adres van de switcher, zodat je het één keer intikt."""
    return os.path.join(data_dir(), "switcher.json")


def load_switcher() -> dict:
    try:
        with open(switcher_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        host = str(data.get("host") or "").strip()
        if host:
            return {"host": host, "port": int(data.get("port") or SWITCHER_PORT)}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def save_switcher(host: str, port: int) -> None:
    try:
        with open(switcher_path(), "w", encoding="utf-8") as fh:
            json.dump({"host": host, "port": int(port)}, fh, indent=2)
    except OSError:
        pass


def load_names() -> dict[int, str]:
    namen = dict(DEFAULT_NAMES)
    try:
        with open(names_path(), "r", encoding="utf-8") as fh:
            opgeslagen = json.load(fh)
        for sleutel, waarde in opgeslagen.items():
            try:
                namen[int(sleutel)] = str(waarde)[:40]
            except (TypeError, ValueError):
                continue
    except (OSError, ValueError):
        pass
    return namen


def save_names(namen: dict[int, str]) -> None:
    try:
        with open(names_path(), "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in sorted(namen.items())}, fh,
                      ensure_ascii=False, indent=2)
    except OSError as fout:
        print(f"  Could not write labels.json: {fout}")


CODE_ALFABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def eigen_adressen() -> set:
    """Alle adressen van deze pc. Windows meldt een verzoek van de pc zelf
    vaak met het LAN-adres in plaats van 127.0.0.1."""
    adressen = {"127.0.0.1", "::1", "localhost"}
    try:
        naam = socket.gethostname()
        for info in socket.getaddrinfo(naam, None):
            adressen.add(str(info[4][0]).split("%")[0])
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        adressen.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    return adressen


def access_path() -> str:
    return os.path.join(data_dir(), "access.json")


class Access:
    """De pc bepaalt wie de pagina mag openen, niet het netwerk."""

    def __init__(self) -> None:
        self.slot = threading.Lock()
        self.devices: dict[str, dict] = {}
        self.pogingen: dict[str, list] = {}
        self.laad()

    def laad(self) -> None:
        try:
            with open(access_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for dev, info in (data.get("devices") or {}).items():
            if isinstance(info, dict) and isinstance(info.get("token"), str):
                self.devices[str(dev)[:64]] = info

    def bewaar(self) -> None:
        try:
            with open(access_path(), "w", encoding="utf-8") as fh:
                json.dump({"devices": self.devices}, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _te_veel(self, ip: str) -> bool:
        nu = time.time()
        recent = [t for t in self.pogingen.get(ip, []) if nu - t < 60]
        self.pogingen[ip] = recent
        if len(recent) >= 20:
            return True
        recent.append(nu)
        return False

    def verzoek(self, dev: str, naam: str, ip: str) -> dict:
        with self.slot:
            info = self.devices.get(dev)
            if info and info.get("status") == "toegestaan":
                info["laatste"] = int(time.time())
                info["ip"] = ip
                if naam:
                    info["naam"] = naam
                return {"status": "toegestaan", "code": info.get("code", ""),
                        "token": info.get("token", "")}
            if self._te_veel(ip):
                return {"status": "te_veel"}
            # ook na een weigering of een verbroken verbinding mag het toestel
            # opnieuw vragen; de pc beslist dan gewoon nog een keer.
            if info and info.get("status") == "geweigerd":
                nu = int(time.time())
                info.update({"status": "wachtend", "laatste": nu, "code": "", "token": ""})
                if naam:
                    info["naam"] = naam
                info["ip"] = ip
                self.bewaar()
                return {"status": "wachtend"}
            nu = int(time.time())
            if info is None:
                info = {"eerste": nu}
                self.devices[dev] = info
            info.update({"naam": naam or "Onbekend toestel", "ip": ip,
                         "status": "wachtend", "laatste": nu, "code": "", "token": ""})
            self.bewaar()
            return {"status": "wachtend"}

    def sta_toe(self, dev: str) -> dict:
        with self.slot:
            info = self.devices.get(dev)
            if not info:
                return {"ok": False}
            code = "".join(secrets.choice(CODE_ALFABET) for _ in range(6))
            info.update({"status": "toegestaan", "code": code,
                         "token": secrets.token_urlsafe(24), "laatste": int(time.time())})
            self.bewaar()
            return {"ok": True, "code": code}

    def weiger(self, dev: str) -> dict:
        with self.slot:
            info = self.devices.get(dev)
            if not info:
                return {"ok": False}
            info.update({"status": "geweigerd", "code": "", "token": ""})
            self.bewaar()
            return {"ok": True}

    def check(self, token: str) -> bool:
        if not token:
            return False
        with self.slot:
            for info in self.devices.values():
                if info.get("status") == "toegestaan" and info.get("token"):
                    if secrets.compare_digest(str(info["token"]), token):
                        info["laatste"] = int(time.time())
                        return True
        return False

    def status(self, dev: str) -> dict:
        with self.slot:
            info = self.devices.get(dev)
            if not info:
                return {"status": "onbekend"}
            stand = {"status": info.get("status", "onbekend")}
            if stand["status"] == "toegestaan":
                stand["code"] = info.get("code", "")
                stand["token"] = info.get("token", "")
            return stand

    def toewijzen(self, dev: str, ingang) -> dict:
        """De pc koppelt een toestel aan een ingang (None = geen)."""
        with self.slot:
            info = self.devices.get(dev)
            if not info:
                return {"ok": False}
            try:
                nummer = int(ingang)
                info["input"] = nummer if nummer > 0 else None
            except (TypeError, ValueError):
                info["input"] = None
            self.bewaar()
            return {"ok": True, "input": info["input"]}

    def input_van(self, token: str):
        """Welke ingang hoort bij deze sleutel? None als er niets is toegewezen."""
        if not token:
            return None
        with self.slot:
            for info in self.devices.values():
                if info.get("status") == "toegestaan" and info.get("token") == token:
                    nummer = info.get("input")
                    return nummer if isinstance(nummer, int) and nummer > 0 else None
        return None

    def lijst(self) -> dict:
        with self.slot:
            nu = int(time.time())
            apparaten = []
            for dev, info in self.devices.items():
                laatste = int(info.get("laatste", nu) or nu)
                apparaten.append({
                    "device": dev,
                    "naam": info.get("naam", ""),
                    "ip": info.get("ip", ""),
                    "status": info.get("status", "onbekend"),
                    "code": info.get("code", ""),
                    "sinds": info.get("eerste", 0),
                    "seconden": max(0, nu - laatste),
                    "input": info.get("input") if isinstance(info.get("input"), int) else None,
                })
            apparaten.sort(key=lambda a: (a["status"] != "wachtend", -a["seconden"]))
            return {"devices": apparaten, "nu": nu}


WATCHED = ("pgmIndex", "pvwIndex", "keyOnAir", "dskOnAir", "ftb", "autoTransition")


# ----------------------------------------------------------------------
# GSP protocol
# ----------------------------------------------------------------------
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def build_packet(command: dict) -> bytes:
    payload = json.dumps(command, separators=(",", ":")).encode("utf-8")
    total = len(payload) + 7
    head = bytes([HEAD1, HEAD2, PROTO]) + struct.pack("<H", total - 5) + payload
    return head + struct.pack("<H", crc16_modbus(head))


def parse_packets(buffer: bytearray) -> list[dict]:
    """Haalt alle complete pakketten uit de buffer; laat een half pakket staan."""
    out: list[dict] = []
    while True:
        start = buffer.find(bytes([HEAD1, HEAD2]))
        if start < 0:
            buffer.clear()
            break
        if start > 0:
            del buffer[:start]
        if len(buffer) < 5:
            break
        body_len = struct.unpack_from("<H", buffer, 3)[0]
        total = body_len + 5
        if len(buffer) < total:
            break
        packet = bytes(buffer[:total])
        del buffer[:total]
        raw = packet[5:total - 2]
        try:
            out.append(json.loads(raw.decode("utf-8", "replace")))
        except Exception:
            continue
    return out


# ----------------------------------------------------------------------
# Gedeelde status
# ----------------------------------------------------------------------
class TallyState:
    def __init__(self, labels: list[str]):
        self.labels = labels
        self.names: dict[int, str] = dict(DEFAULT_NAMES)
        self.aliases: dict[int, int] = dict(SOURCE_ALIASES)
        self.values: dict[str, object] = {
            "pgmIndex": 0,
            "pvwIndex": 0,
            "keyOnAir": 0,
            "dskOnAir": 0,
            "ftb": 0,
            "autoTransition": 0,
        }
        self.connected = False
        self.switcher = ""
        self.paused = False
        self.doel_teller = 0      # ophogen = opnieuw verbinden met het nieuwe adres
        self.local_prefix = ""
        self.configured_host = ""
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []

    def _snap(self) -> dict:
        return {
            "connected": self.connected,
            "switcher": self.switcher,
            "paused": self.paused,
            "labels": self.labels,
            "names": {str(k): v for k, v in self.names.items()},
            "version": VERSION,
            "values": dict(self.values),
            "ts": time.time(),
        }

    def snapshot(self) -> dict:
        with self._lock:
            return self._snap()

    def push_now(self) -> None:
        """Stuur de huidige stand meteen naar alle open verbindingen.
        Nodig als er iets verandert dat niet van de switcher komt,
        bijvoorbeeld een net toegewezen ingang."""
        self._publish(self.snapshot())

    def _publish(self, snap: dict) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(snap)
            except queue.Full:
                pass

    def update(self, data: dict) -> bool:
        """Verwerkt één commando. Geeft True als er iets zichtbaar veranderde."""
        pid = data.get("id")
        if pid not in WATCHED:
            return False
        value = data.get("value") or []
        new = value[0] if value else 0
        if pid in ("pgmIndex", "pvwIndex"):
            # AUX 1-4 van de Duet 8 zijn in werkelijkheid ingang 5-8
            new = self.aliases.get(new, new)
        with self._lock:
            if self.values.get(pid) == new:
                return False
            self.values[pid] = new
            snap = self._snap()
        self._publish(snap)
        return True

    def set_connected(self, connected: bool, switcher: str = "") -> None:
        with self._lock:
            self.connected = connected
            self.switcher = switcher
            snap = self._snap()
        self._publish(snap)

    def name_for(self, source: object) -> str:
        try:
            nummer = int(source)
        except (TypeError, ValueError):
            return f"Bron {source}"
        return self.names.get(nummer) or f"Bron {nummer}"

    def set_names(self, namen: dict[int, str]) -> None:
        with self._lock:
            self.names.update(namen)
            # de vaste rij tegels volgt de namen, anders blijft de oude tekst staan
            self.labels = [self.name_for(i) for i in range(1, len(self.labels) + 1)]
            snap = self._snap()
        self._publish(snap)

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.paused = paused
            snap = self._snap()
        self._publish(snap)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=32)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


# ----------------------------------------------------------------------
# Instructies in alle talen (los te openen en via /instructies)
# ----------------------------------------------------------------------
INSTRUCTIES: dict[str, dict] = {
    "pap": {"titel": "Kon bo e tally", "blokken": [
        ("Kiko e ta", "E tally ta mustra riba bo telefòn kua fuente ta na PREVIEW i kua ta ON AIR."),
        ("Kuminsá", "Habri e programa. E ta habri e página di akseso den bo browser. E bentana mester keda habrí."),
        ("Direkshon di e switcher", "E bridge ta buska e switcher su mes. Si e no hañ'é, skirbi e direkshon (por ehèmpel 192.168.1.232) i primi Warda."),
        ("Bo telefòn", "Habri e direkshon ku ta aparecé den e bentana riba bo telefòn. Pidi akseso i laga e PC aprobá e aparato."),
        ("Si e no konektá", "Kontrulá ku e telefòn ta riba e mesun rede. Riba Windows, permití e programa den e firewall."),
        ("Sin Python", "E programa .exe no mester Python. Kòrká riba dje dos biaha. E bentana ku e direkshon mester keda habrí."),
    ]},
    "nl": {"titel": "Zo werkt de tally", "blokken": [
        ("Wat het doet", "De tally toont op je telefoon welke bron in PREVIEW staat en welke ON AIR is."),
        ("Starten", "Open het programma. Het opent zelf de beheerpagina in je browser. Laat dat venster openstaan."),
        ("Adres van de switcher", "De bridge zoekt de switcher zelf. Vindt hij hem niet, typ dan het adres (bijvoorbeeld 192.168.1.232) en druk op Opslaan."),
        ("Je telefoon", "Open het adres uit het venster op je telefoon. Vraag toegang en keur het toestel goed op de pc."),
        ("Als het niet verbindt", "Controleer of de telefoon op hetzelfde netwerk zit. Sta op Windows het programma toe in de firewall."),
        ("Zonder Python", "Het .exe-programma heeft geen Python nodig. Dubbelklik erop. Het venster met het adres blijft open."),
    ]},
    "en": {"titel": "How the tally works", "blokken": [
        ("What it does", "The tally shows on your phone which source is in PREVIEW and which is ON AIR."),
        ("Starting", "Open the program. It opens the control page in your browser. Keep that window open."),
        ("Switcher address", "The bridge looks for the switcher itself. If it cannot find it, type the address (for example 192.168.1.232) and press Save."),
        ("Your phone", "Open the address from the window on your phone. Request access and approve the device on the PC."),
        ("If it does not connect", "Check that the phone is on the same network. On Windows, allow the program in the firewall."),
        ("Without Python", "The .exe program needs no Python. Double-click it. The window with the address stays open."),
    ]},
    "es": {"titel": "Cómo funciona el tally", "blokken": [
        ("Qué hace", "El tally muestra en tu teléfono qué fuente está en PREVIEW y cuál en ON AIR."),
        ("Iniciar", "Abre el programa. Abre la página de control en tu navegador. Deja esa ventana abierta."),
        ("Dirección del switcher", "El puente busca el switcher por sí mismo. Si no lo encuentra, escribe la dirección (por ejemplo 192.168.1.232) y pulsa Guardar."),
        ("Tu teléfono", "Abre en tu teléfono la dirección de la ventana. Pide acceso y aprueba el dispositivo en el PC."),
        ("Si no conecta", "Comprueba que el teléfono está en la misma red. En Windows, permite el programa en el cortafuegos."),
        ("Sin Python", "El programa .exe no necesita Python. Haz doble clic. La ventana con la dirección queda abierta."),
    ]},
    "fr": {"titel": "Comment fonctionne le tally", "blokken": [
        ("Ce qu'il fait", "Le tally montre sur votre téléphone quelle source est en PREVIEW et laquelle est ON AIR."),
        ("Démarrer", "Ouvrez le programme. Il ouvre la page de contrôle dans votre navigateur. Laissez cette fenêtre ouverte."),
        ("Adresse du switcher", "Le pont cherche le switcher lui-même. S'il ne le trouve pas, saisissez l'adresse (par exemple 192.168.1.232) et appuyez sur Enregistrer."),
        ("Votre téléphone", "Ouvrez sur votre téléphone l'adresse affichée dans la fenêtre. Demandez l'accès et approuvez l'appareil sur le PC."),
        ("Si ça ne connecte pas", "Vérifiez que le téléphone est sur le même réseau. Sous Windows, autorisez le programme dans le pare-feu."),
        ("Sans Python", "Le programme .exe n'a pas besoin de Python. Double-cliquez dessus. La fenêtre avec l'adresse reste ouverte."),
    ]},
    "de": {"titel": "So funktioniert die Tally", "blokken": [
        ("Was sie macht", "Die Tally zeigt auf Ihrem Telefon, welche Quelle in PREVIEW und welche ON AIR ist."),
        ("Starten", "Öffnen Sie das Programm. Es öffnet die Steuerseite im Browser. Lassen Sie dieses Fenster offen."),
        ("Adresse des Switchers", "Die Bridge sucht den Switcher selbst. Findet sie ihn nicht, tragen Sie die Adresse ein (zum Beispiel 192.168.1.232) und drücken Sie Speichern."),
        ("Ihr Telefon", "Öffnen Sie die Adresse aus dem Fenster auf Ihrem Telefon. Fragen Sie Zugang an und bestätigen Sie das Gerät am PC."),
        ("Wenn keine Verbindung entsteht", "Prüfen Sie, ob das Telefon im selben Netzwerk ist. Erlauben Sie das Programm in der Windows-Firewall."),
        ("Ohne Python", "Das .exe-Programm braucht kein Python. Doppelklicken Sie darauf. Das Fenster mit der Adresse bleibt offen."),
    ]},
    "pt": {"titel": "Como funciona o tally", "blokken": [
        ("O que faz", "O tally mostra no seu telefone qual fonte está em PREVIEW e qual está ON AIR."),
        ("Iniciar", "Abra o programa. Ele abre a página de controlo no navegador. Deixe essa janela aberta."),
        ("Endereço do switcher", "A ponte procura o switcher sozinha. Se não o encontrar, escreva o endereço (por exemplo 192.168.1.232) e prima Guardar."),
        ("O seu telefone", "Abra no telefone o endereço da janela. Peça acesso e aprove o dispositivo no PC."),
        ("Se não ligar", "Verifique se o telefone está na mesma rede. No Windows, permita o programa na firewall."),
        ("Sem Python", "O programa .exe não precisa de Python. Faça duplo clique. A janela com o endereço fica aberta."),
    ]},
    "it": {"titel": "Come funziona il tally", "blokken": [
        ("Cosa fa", "Il tally mostra sul telefono quale sorgente è in PREVIEW e quale è ON AIR."),
        ("Avviare", "Apri il programma. Apre la pagina di controllo nel browser. Lascia aperta quella finestra."),
        ("Indirizzo dello switcher", "Il bridge cerca lo switcher da solo. Se non lo trova, inserisci l'indirizzo (per esempio 192.168.1.232) e premi Salva."),
        ("Il tuo telefono", "Apri sul telefono l'indirizzo della finestra. Chiedi accesso e approva il dispositivo sul PC."),
        ("Se non si connette", "Controlla che il telefono sia sulla stessa rete. Su Windows, consenti il programma nel firewall."),
        ("Senza Python", "Il programma .exe non richiede Python. Fai doppio clic. La finestra con l'indirizzo resta aperta."),
    ]},
    "zh": {"titel": "计时器使用说明", "blokken": [
        ("作用", "计时器在手机上显示哪个信号源在 PREVIEW，哪个在 ON AIR。"),
        ("启动", "打开程序。它会在浏览器中打开控制页面。请保持该窗口打开。"),
        ("切换台地址", "桥接会自动查找切换台。如果找不到，请输入地址（例如 192.168.1.232）并按保存。"),
        ("你的手机", "在手机上打开窗口中显示的地址。申请访问，并在电脑上批准该设备。"),
        ("无法连接时", "检查手机是否在同一网络。在 Windows 防火墙中允许该程序。"),
        ("无需 Python", "exe 程序不需要 Python。双击即可。显示地址的窗口会保持打开。"),
    ]},
    "ja": {"titel": "タリーの使い方", "blokken": [
        ("できること", "タリーは、どのソースが PREVIEW でどれが ON AIR かをスマートフォンに表示します。"),
        ("起動", "プログラムを開きます。ブラウザーに操作ページが開きます。このウィンドウは開いたままにしてください。"),
        ("スイッチャーのアドレス", "ブリッジはスイッチャーを自動で探します。見つからない場合はアドレス（例 192.168.1.232）を入力して保存を押します。"),
        ("スマートフォン", "ウィンドウに表示されたアドレスをスマートフォンで開きます。アクセスを要求し、PC 側で承認します。"),
        ("接続しない場合", "スマートフォンが同じネットワークにあるか確認してください。Windows のファイアウォールでプログラムを許可します。"),
        ("Python 不要", "exe プログラムに Python は不要です。ダブルクリックしてください。アドレスのウィンドウは開いたままにします。"),
    ]},
    "ko": {"titel": "탈리 사용 방법", "blokken": [
        ("기능", "탈리는 어떤 소스가 PREVIEW 이고 어떤 소스가 ON AIR 인지 휴대폰에 보여 줍니다."),
        ("시작", "프로그램을 엽니다. 브라우저에 제어 페이지가 열립니다. 그 창은 열어 두세요."),
        ("스위처 주소", "브리지가 스위처를 자동으로 찾습니다. 찾지 못하면 주소(예: 192.168.1.232)를 입력하고 저장을 누르세요."),
        ("휴대폰", "창에 표시된 주소를 휴대폰에서 엽니다. 접근을 요청하고 PC에서 기기를 승인하세요."),
        ("연결되지 않을 때", "휴대폰이 같은 네트워크에 있는지 확인하세요. Windows 방화벽에서 프로그램을 허용하세요."),
        ("Python 불필요", "exe 프로그램에는 Python이 필요 없습니다. 두 번 클릭하세요. 주소가 있는 창은 열어 두세요."),
    ]},
    "ru": {"titel": "Как работает талли", "blokken": [
        ("Что делает", "Талли показывает на телефоне, какой источник в PREVIEW, а какой в ON AIR."),
        ("Запуск", "Откройте программу. Она откроет страницу управления в браузере. Это окно должно остаться открытым."),
        ("Адрес свитчера", "Мост ищет свитчер сам. Если не находит, введите адрес (например 192.168.1.232) и нажмите Сохранить."),
        ("Ваш телефон", "Откройте на телефоне адрес из окна. Запросите доступ и подтвердите устройство на ПК."),
        ("Если нет подключения", "Проверьте, что телефон в той же сети. В Windows разрешите программу в брандмауэре."),
        ("Без Python", "Программе .exe Python не нужен. Дважды щёлкните по ней. Окно с адресом остаётся открытым."),
    ]},
    "ar": {"titel": "كيف يعمل العدّاد", "blokken": [
        ("ما يفعله", "يعرض العدّاد على هاتفك أي مصدر في PREVIEW وأي مصدر في ON AIR."),
        ("البدء", "افتح البرنامج. سيفتح صفحة التحكم في المتصفح. اترك تلك النافذة مفتوحة."),
        ("عنوان المبدّل", "يبحث الجسر عن المبدّل بنفسه. إن لم يجده، أدخل العنوان (مثل 192.168.1.232) واضغط حفظ."),
        ("هاتفك", "افتح على هاتفك العنوان الظاهر في النافذة. اطلب الوصول ووافق على الجهاز من الحاسوب."),
        ("إذا لم يتصل", "تأكد أن الهاتف على الشبكة نفسها. في Windows اسمح للبرنامج في جدار الحماية."),
        ("بدون Python", "برنامج exe لا يحتاج إلى Python. انقر عليه نقرًا مزدوجًا. تبقى نافذة العنوان مفتوحة."),
    ]},
    "hi": {"titel": "टैली कैसे काम करता है", "blokken": [
        ("यह क्या करता है", "टैली आपके फ़ोन पर दिखाता है कि कौन सा स्रोत PREVIEW में है और कौन ON AIR में।"),
        ("शुरू करें", "प्रोग्राम खोलें। यह ब्राउज़र में नियंत्रण पेज खोलता है। उस विंडो को खुला रखें।"),
        ("स्विचर का पता", "ब्रिज स्विचर को स्वयं खोजता है। न मिले तो पता (जैसे 192.168.1.232) लिखें और सेव दबाएँ।"),
        ("आपका फ़ोन", "विंडो में दिखा पता अपने फ़ोन में खोलें। एक्सेस माँगें और पीसी पर डिवाइस स्वीकार करें।"),
        ("अगर कनेक्ट न हो", "जाँचें कि फ़ोन उसी नेटवर्क पर है। Windows फ़ायरवॉल में प्रोग्राम को अनुमति दें।"),
        ("Python के बिना", "exe प्रोग्राम के लिए Python की ज़रूरत नहीं। उस पर डबल-क्लिक करें। पता वाली विंडो खुली रहती है।"),
    ]},
    "id": {"titel": "Cara kerja tally", "blokken": [
        ("Fungsinya", "Tally menampilkan di ponsel Anda sumber mana yang PREVIEW dan mana yang ON AIR."),
        ("Menjalankan", "Buka program. Ia membuka halaman kendali di browser. Biarkan jendela itu tetap terbuka."),
        ("Alamat switcher", "Bridge mencari switcher sendiri. Jika tidak menemukannya, ketik alamatnya (misalnya 192.168.1.232) lalu tekan Simpan."),
        ("Ponsel Anda", "Buka alamat dari jendela itu di ponsel. Minta akses dan setujui perangkat di PC."),
        ("Jika tidak terhubung", "Periksa apakah ponsel berada di jaringan yang sama. Di Windows, izinkan program di firewall."),
        ("Tanpa Python", "Program .exe tidak memerlukan Python. Klik dua kali. Jendela berisi alamat tetap terbuka."),
    ]},
}

KIES_TEKST = {'pap': 'Skohe bo idioma', 'nl': 'Kies je taal', 'en': 'Choose your language', 'es': 'Elige tu idioma', 'fr': 'Choisissez votre langue', 'de': 'Wählen Sie Ihre Sprache', 'pt': 'Escolha o seu idioma', 'it': 'Scegli la tua lingua', 'zh': '选择你的语言', 'ja': '言語を選んでください', 'ko': '언어를 선택하세요', 'ru': 'Выберите язык', 'ar': 'اختر لغتك', 'hi': 'अपनी भाषा चुनें', 'id': 'Pilih bahasa Anda'}

TAAL_NAMEN = [("pap", "Papiamentu"), ("nl", "Nederlands"), ("en", "English"), ("es", "Español"),
              ("fr", "Français"), ("de", "Deutsch"), ("pt", "Português"), ("it", "Italiano"),
              ("zh", "中文"), ("ja", "日本語"), ("ko", "한국어"), ("ru", "Русский"),
              ("ar", "العربية"), ("hi", "हिन्दी"), ("id", "Bahasa Indonesia")]


def instructies_html() -> str:
    """De instructiepagina: eerst de taal kiezen, dan lezen."""
    namen = json.dumps([{"kode": k, "naam": n} for k, n in TAAL_NAMEN], ensure_ascii=False)
    data = json.dumps(INSTRUCTIES, ensure_ascii=False)
    kies = json.dumps(KIES_TEKST, ensure_ascii=False)
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GoStream Tally</title>
<style>
  body { margin: 0; padding: 26px 20px 60px; background: #0b0b0f; color: #e6e6ee;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif; line-height: 1.6; }
  .doos { max-width: 640px; margin: 0 auto; }
  .kop { display: flex; align-items: center; gap: 12px; margin-bottom: 22px;
    padding-bottom: 14px; border-bottom: 1px solid #24242f; }
  h1 { font-size: 20px; margin: 0; flex: 1; }
  h2 { font-size: 14px; margin: 22px 0 4px; color: #79c8ff; letter-spacing: .03em; }
  p { margin: 0; color: #c2c2cf; font-size: 14px; }
  .voet { margin-top: 34px; color: #55555f; font-size: 12px; }
  button.taal { font-family: inherit; font-size: 15px; font-weight: 600; padding: 15px 14px;
    border-radius: 11px; border: 1px solid #2e2e3c; background: #15151c; color: #e6e6ee;
    cursor: pointer; text-align: left; }
  button.taal:hover { background: #1f1f2b; border-color: #3d3d50; }
  button.wissel { font-family: inherit; font-size: 12px; font-weight: 600; padding: 7px 11px;
    border-radius: 8px; border: 1px solid #2e2e3c; background: #1c1c26; color: #dcdce6; cursor: pointer; }
  button.wissel:hover { background: #262633; }
  .raster { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; margin-top: 24px; }
  .kiesdoos { max-width: 700px; margin: 6vh auto 0; }
  .kiesdoos h1 { font-size: 22px; margin-bottom: 0; }
</style>
</head>
<body>

<div id="kies" class="kiesdoos">
  <h1 id="kiesTitel"></h1>
  <div id="knoppen" class="raster"></div>
</div>

<div id="lees" class="doos" hidden>
  <div class="kop">
    <h1 id="titel"></h1>
    <button class="wissel" id="wissel"></button>
  </div>
  <div id="inhoud"></div>
  <p class="voet">GoStream Tally</p>
</div>

<script>
const DATA = __DATA__;
const TALEN = __TALEN__;
const KIES = __KIES__;

const kiesVak = document.getElementById('kies');
const leesVak = document.getElementById('lees');
let taal = localStorage.getItem('instrTaal');

function systeemTaal() {
  const s = (navigator.language || 'en').slice(0, 2).toLowerCase();
  return DATA[s] ? s : 'en';
}

function toonKiezer() {
  const voorstel = taal && DATA[taal] ? taal : systeemTaal();
  document.getElementById('kiesTitel').textContent = KIES[voorstel] || KIES.en;
  document.documentElement.lang = voorstel;
  document.documentElement.dir = (voorstel === 'ar') ? 'rtl' : 'ltr';
  const vak = document.getElementById('knoppen');
  vak.textContent = '';
  TALEN.forEach(t => {
    const b = document.createElement('button');
    b.className = 'taal';
    b.textContent = t.naam;
    b.onclick = () => { taal = t.kode; localStorage.setItem('instrTaal', taal); toonLezer(); };
    // de naam in de eigen taal, zodat iedereen zijn eigen regel herkent
    b.setAttribute('lang', t.kode);
    vak.append(b);
  });
  kiesVak.hidden = false;
  leesVak.hidden = true;
}

function toonLezer() {
  const d = DATA[taal] || DATA.en;
  document.getElementById('titel').textContent = d.titel;
  const eigen = TALEN.find(t => t.kode === taal);
  document.getElementById('wissel').textContent = (eigen ? eigen.naam : 'English') + ' \u25be';
  document.documentElement.lang = taal;
  document.documentElement.dir = (taal === 'ar') ? 'rtl' : 'ltr';
  const vak = document.getElementById('inhoud');
  vak.textContent = '';
  d.blokken.forEach(([kop, tekst]) => {
    const h = document.createElement('h2');
    h.textContent = kop;
    const p = document.createElement('p');
    p.textContent = tekst;
    vak.append(h, p);
  });
  kiesVak.hidden = true;
  leesVak.hidden = false;
}

document.getElementById('wissel').onclick = toonKiezer;

// een keuze van een vorige keer telt; anders eerst kiezen
if (taal && DATA[taal]) { toonLezer(); } else { toonKiezer(); }
</script>
</body>
</html>
""".replace("__DATA__", data).replace("__TALEN__", namen).replace("__KIES__", kies)


# ----------------------------------------------------------------------
# Switcher-client
# ----------------------------------------------------------------------
class Target:
    """Het adres van de switcher; kan onderweg door een netwerkscan worden bijgesteld."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.found_differently = False
        self.last_scan = 0.0


def probe(host: str, port: int, timeout: float = 0.4) -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return host
    except OSError:
        return None
    finally:
        sock.close()


def find_switcher(prefix: str, port: int) -> str | None:
    """Zoekt in het eigen subnet naar een host met de switcher-poort open."""
    hosts = [f"{prefix}.{i}" for i in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for result in pool.map(lambda h: probe(h, port), hosts):
            if result:
                return result
    return None


def switcher_loop(state: TallyState, target: Target, stop: threading.Event) -> None:
    mislukt = 0
    while not stop.is_set():
        if state.paused:
            # verbinding bewust vrijgegeven, bijvoorbeeld voor de GoStream-pc-software
            state.set_connected(False, "", )
            time.sleep(0.3)
            continue

        # Na twee mislukte pogingen zelf het netwerk afzoeken: het IP kan door
        # DHCP zijn veranderd, of het toestel staat op een ander adres.
        if (mislukt >= 2 and not target.found_differently and state.local_prefix
                and time.time() - target.last_scan > 120):
            target.last_scan = time.time()
            state.set_connected(False, "netwerk afzoeken...")
            gevonden = find_switcher(state.local_prefix, target.port)
            if gevonden and gevonden != target.host:
                target.host = gevonden
                target.found_differently = True
                print(f"  Switcher found at {gevonden} (was {state.configured_host})")
                sys.stdout.flush()

        host, port = target.host, target.port
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.settimeout(1.0)
            state.set_connected(True, f"{host}:{port}")
            mislukt = 0
            for pid in WATCHED:
                sock.sendall(build_packet({"id": pid, "type": "get"}))
            buffer = bytearray()
            last_ping = time.time()
            telling = state.doel_teller
            while not stop.is_set() and not state.paused:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    chunk = b""
                    if time.time() - last_ping > 5:
                        # geen keep-alive in het protocol: af en toe opnieuw opvragen
                        for pid in WATCHED:
                            sock.sendall(build_packet({"id": pid, "type": "get"}))
                        last_ping = time.time()
                if chunk == b"" and not isinstance(chunk, bytes):
                    break
                if chunk:
                    buffer.extend(chunk)
                    for command in parse_packets(buffer):
                        state.update(command)
                if state.doel_teller != telling:
                    break          # er is een nieuw adres ingesteld
        except (OSError, socket.error):
            mislukt += 1
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if not state.connected:
                state.set_connected(False, f"{host}: geen antwoord")
        if stop.is_set():
            break
        time.sleep(2)  # opnieuw proberen


def demo_loop(state: TallyState, labels: list[str], stop: threading.Event) -> None:
    """Simuleert een switcher, zodat de pagina zonder hardware getest kan worden."""
    state.set_connected(True, "demo")
    sources = list(range(1, max(2, len(labels) + 1)))
    index = 0
    while not stop.is_set():
        pgm = sources[index % len(sources)]
        pvw = sources[(index + 1) % len(sources)]
        state.update({"id": "pgmIndex", "value": [pgm]})
        state.update({"id": "pvwIndex", "value": [pvw]})
        index += 1
        for _ in range(30):
            if stop.is_set():
                return
            time.sleep(0.1)


# ----------------------------------------------------------------------
# Webpagina
# ----------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0b0f">
<title>GoStream Tally</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; height: 100%; background: #0b0b0f; color: #f2f2f5;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { display: flex; flex-direction: column; overflow: hidden; }
  #bar { display: flex; align-items: center; justify-content: space-between;
    padding: 10px 14px; font-size: 13px; letter-spacing: .06em; text-transform: uppercase;
    background: #16161d; border-bottom: 1px solid #24242e; }
  #conn { display: flex; align-items: center; gap: 7px; color: #8b8b98; }
  #dot { width: 9px; height: 9px; border-radius: 50%; background: #c0392b; }
  #dot.on { background: #2ecc71; box-shadow: 0 0 8px #2ecc7188; }
  #meta { display: flex; align-items: center; gap: 9px; }
  #version { color: #4d4d5c; font-size: 11px; letter-spacing: .04em; }
  #lang { background: #1b1b24; border: 1px solid #2a2a36; color: #cfcfda; border-radius: 7px;
    font-size: 11px; font-weight: 700; padding: 4px 7px; letter-spacing: .06em; font-family: inherit; }
  #taalscherm { position: fixed; inset: 0; background: #0b0b0ff5; display: none;
    flex-direction: column; padding: 14px; gap: 10px; z-index: 30; }
  #taalscherm.open { display: flex; }
  #toegang { position: fixed; inset: 0; background: #0b0b0ff8; display: none;
    align-items: center; justify-content: center; padding: 20px; z-index: 40; }
  #toegang.open { display: flex; }
  #toegang .kaart { max-width: 340px; width: 100%; text-align: center; }
  #toegang .slot { font-size: 34px; margin-bottom: 10px; }
  #toegang h2 { margin: 0 0 10px; font-size: 19px; }
  #toegang p { color: #9a9aa8; font-size: 14px; line-height: 1.55; margin: 0 0 18px; }
  #toegang button { width: 100%; padding: 14px; font-size: 15px; font-weight: 700;
    background: #1f5a85; border: 1px solid #2d86c4; color: #fff; border-radius: 9px; }
  #toegang button:disabled { opacity: .55; }
  .codLabel { font-size: 11px; text-transform: uppercase; letter-spacing: .16em; color: #7a7a8a; margin-top: 16px; }
  .codGroot { font-family: ui-monospace, Consolas, monospace; font-size: 34px; font-weight: 700;
    letter-spacing: .16em; color: #79c8ff; margin: 8px 0 10px; }
  .codUitleg { font-size: 13px; color: #9a9aa8; line-height: 1.5; }
  #taalscherm h2 { margin: 0; font-size: 15px; letter-spacing: .1em; text-transform: uppercase; color: #9a9aa8; }
  #taallijst { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; overflow: auto; flex: 1; align-content: start; }
  #taallijst button { text-align: left; padding: 12px 10px; font-size: 14px; }
  #taallijst button.actief { background: #1f5a85; border-color: #2d86c4; color: #ffffff; font-weight: 700; }
  #grid { flex: 1; display: grid; gap: 8px; padding: 8px; grid-template-columns: 1fr 1fr; }
  .tile { position: relative; border-radius: 14px; background: #17171f; border: 1px solid #262633;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    overflow: hidden; transition: background .12s, border-color .12s, transform .12s; }
  .tile .name { font-size: clamp(15px, 5.2vw, 26px); font-weight: 700; letter-spacing: .02em; }
  .tile .role { font-size: clamp(11px, 3.2vw, 15px); font-weight: 700; letter-spacing: .14em;
    text-transform: uppercase; margin-top: 5px; opacity: 0; }
  .tile.pgm { background: #d92b2b; border-color: #ff6b6b; box-shadow: 0 0 26px #d92b2b66 inset; }
  .tile.pgm .role { opacity: 1; }
  .tile.pvw { background: #12853f; border-color: #3ddc84; }
  .tile.pvw .role { opacity: 1; }
  .tile.pgm.pvw { background: #12853f; border-color: #3ddc84; }
  .tile.pgm.pvw::after { content: ""; position: absolute; inset: 0;
    background: repeating-linear-gradient(45deg, #d92b2b33 0 14px, transparent 14px 28px); }
  .tile.flash { transform: scale(1.03); }
  footer { display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 10px 14px; background: #16161d; border-top: 1px solid #24242e; font-size: 12px; color: #7c7c8a; }
  button { font: inherit; color: #d8d8e2; background: #23232f; border: 1px solid #33333f;
    border-radius: 9px; padding: 7px 12px; }
  button.active { background: #2ecc71; border-color: #2ecc71; color: #06180d; font-weight: 700; }
  #hint { white-space: nowrap; }
  footer button { flex: none; }
  /* smalle telefoon: de uitleg wijkt, zodat alle knoppen bereikbaar blijven */
  @media (max-width: 470px) {
    #hint { display: none; }
    footer { gap: 7px; padding: 9px 10px; }
    footer button { flex: 1; padding: 9px 6px; text-align: center; }
  }
  /* rand om het hele scherm: groen bij preview, rood bij on air */
  #rand { position: fixed; inset: 0; pointer-events: none; z-index: 8;
    border: 0 solid transparent; transition: border-color .12s ease; }
  #rand.pvw { border: 14px solid #2fbf5f; }
  #rand.pgm { border: 14px solid #e23131; }
  #eigen { font-size: 11px; letter-spacing: .06em; padding: 3px 8px;
    border-radius: 6px; background: #1b1b24; color: #cfcfda; }
  #flits { position: fixed; inset: 0; pointer-events: none; background: #ffffff;
    opacity: 0; transition: opacity .1s; z-index: 20; }
  #flits.aan { opacity: .62; transition: none; }
  #editor { position: fixed; inset: 0; background: #0b0b0ff2; display: none;
    flex-direction: column; padding: 14px; gap: 10px; z-index: 10; }
  #editor.open { display: flex; }
  #editor h2 { margin: 0; font-size: 15px; letter-spacing: .1em; text-transform: uppercase; color: #9a9aa8; }
  #rows { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
  .row { display: flex; align-items: center; gap: 10px; }
  .row span { width: 62px; flex: none; font-size: 12px; color: #6e6e7e; font-variant-numeric: tabular-nums; }
  .row input { flex: 1; font: inherit; color: #eaeaef; background: #17171f;
    border: 1px solid #2b2b38; border-radius: 8px; padding: 9px 11px; }
  #editactions { display: flex; gap: 10px; justify-content: flex-end; }
</style>
</head>
<body>
<div id="bar">
  <div id="conn"><span id="dot"></span><span id="connlabel">verbinden…</span></div>
  <div id="meta"><span id="eigen" hidden></span><span id="switcher"></span><span id="version"></span><button id="lang">NL</button></div>
</div>
<div id="grid"></div>
<footer>
  <span id="hint">PGM rood, PVW groen</span>
  <button id="names">Namen</button>
  <button id="release">Geef vrij</button>
  <button id="buzz">Melding</button>
</footer>
<div id="rand"></div>
<div id="flits"></div>
<div id="toegang">
  <div class="kaart">
    <div class="slot">&#128274;</div>
    <h2 id="toegangTitel">Toegang nodig</h2>
    <p id="toegangTekst">Deze tally is beveiligd.</p>
    <button id="toegangVraag">Toegang vragen</button>
    <div id="toegangCode"></div>
  </div>
</div>
<div id="taalscherm">
  <h2 id="taalTitel">Taal</h2>
  <div id="taallijst"></div>
  <div id="editactions"><button id="taalsluit">Sluiten</button></div>
</div>
<div id="editor">
  <h2>Namen van de bronnen</h2>
  <div id="rows"></div>
  <div id="editactions">
    <button id="edcancel">Annuleren</button>
    <button id="edsave">Opslaan</button>
  </div>
</div>
<script>
const grid = document.getElementById('grid');
const dot = document.getElementById('dot');
const connlabel = document.getElementById('connlabel');
const switcherEl = document.getElementById('switcher');
const versionEl = document.getElementById('version');
const toegangEl = document.getElementById('toegang');
const toegangTitel = document.getElementById('toegangTitel');
const toegangTekst = document.getElementById('toegangTekst');
const toegangVraag = document.getElementById('toegangVraag');
const toegangCode = document.getElementById('toegangCode');

let token = localStorage.getItem('tallyToken') || '';
let deviceId = localStorage.getItem('tallyDevice') || '';
let toegangStand = 'uitleg';
let pollTimer = null;
let es = null;

function maakDeviceId() {
  const bytes = new Uint8Array(16);
  if (window.crypto && window.crypto.getRandomValues) {
    window.crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
}
if (!deviceId) {
  deviceId = maakDeviceId();
  localStorage.setItem('tallyDevice', deviceId);
}

function tt(sleutel) {
  const bron = TOEGANG[huidigeTaal] || TOEGANG.en;
  return bron[sleutel] !== undefined ? bron[sleutel] : TOEGANG.nl[sleutel];
}

function api(pad, opties) {
  opties = opties || {};
  opties.headers = Object.assign({}, opties.headers || {}, { 'X-Tally-Token': token });
  return fetch(pad, opties);
}

function zetToegangStand(stand) {
  toegangStand = stand;
  toegangTitel.textContent = tt('titel');
  toegangVraag.textContent = tt('vraag');
  if (stand === 'uitleg') toegangTekst.textContent = tt('uitleg');
  if (stand === 'wacht') toegangTekst.textContent = tt('wacht');
  if (stand === 'nee') toegangTekst.textContent = tt('nee');
  toegangVraag.disabled = (stand === 'wacht');
}

function toonToegang() {
  if (es) { es.close(); es = null; }
  stopPoll();
  toegangCode.textContent = '';
  toegangEl.classList.add('open');
  zetToegangStand('uitleg');
}

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function startPoll() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/access/poll?device=' + encodeURIComponent(deviceId));
      verwerkToegang(await r.json());
    } catch (_) { /* volgende ronde */ }
  }, 2000);
}

function verwerkToegang(d) {
  if (d.status === 'toegestaan') {
    stopPoll();
    token = d.token;
    localStorage.setItem('tallyToken', token);
    toegangTekst.textContent = '';
    toegangVraag.disabled = true;
    toegangCode.textContent = '';
    const label = document.createElement('div');
    label.className = 'codLabel';
    label.textContent = 'code';
    const groot = document.createElement('div');
    groot.className = 'codGroot';
    groot.textContent = d.code || '';
    const uitleg = document.createElement('div');
    uitleg.className = 'codUitleg';
    uitleg.textContent = tt('cod');
    toegangCode.append(label, groot, uitleg);
    setTimeout(() => {
      toegangEl.classList.remove('open');
      startTally();
    }, 2400);
    return;
  }
  if (d.status === 'wachtend') {
    zetToegangStand('wacht');
    startPoll();
    return;
  }
  if (d.status === 'geweigerd') {
    stopPoll();
    zetToegangStand('nee');
    toegangVraag.disabled = false;
    return;
  }
  if (d.status === 'te_veel') {
    stopPoll();
    toegangTekst.textContent = 'Te veel aanvragen. Wacht een minuut.';
    toegangVraag.disabled = false;
  }
}

function toestelNaam() {
  const ua = navigator.userAgent || '';
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua)) return 'iPad';
  if (/Android/.test(ua)) return 'Android';
  if (/Macintosh/.test(ua)) return 'Mac';
  if (/Windows/.test(ua)) return 'Windows';
  return 'Toestel';
}

toegangVraag.addEventListener('click', async () => {
  toegangVraag.disabled = true;
  zetToegangStand('wacht');
  try {
    const r = await fetch('/access/request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device: deviceId, naam: toestelNaam() })
    });
    verwerkToegang(await r.json());
  } catch (_) {
    toegangTekst.textContent = tt('uitleg');
    toegangVraag.disabled = false;
  }
});


// ---------------------------------------------------------------
// Talen. De Papiamentu-vormen volgen de Buki di Oro (FPI 2009).
// ON AIR en PREVIEW blijven staan: dat is vaktaal in de regiekamer.
// ---------------------------------------------------------------
const TEKST = {
  pap: { eigenNaam: 'Papiamentu', kode: 'PAP',
    verbinden: 'ta konektá…', verbonden: 'konektá', geenVerbinding: 'sin konekshon',
    vrijgegeven: 'liberá', switcherVrij: 'switcher liber', opnieuw: 'ta konektá atrobe…',
    geefVrij: 'Liberá', verbind: 'Konektá',
    flitsAan: 'Alerta aktivo', flitsUit: 'Alerta inaktivo', flitsTril: 'Alerta + vibra',
    namen: 'Nòmbernan', namenTitel: 'Nòmbernan di e bronnan',
    annuleer: 'Kanselá', opslaan: 'Warda', hint: 'PGM kòrá, PVW bèrdè',
    bron: 'Bron', taal: 'Idioma', sluit: 'Sera' },
  nl: { eigenNaam: 'Nederlands', kode: 'NL',
    verbinden: 'verbinden…', verbonden: 'verbonden', geenVerbinding: 'geen verbinding',
    vrijgegeven: 'vrijgegeven', switcherVrij: 'switcher vrij', opnieuw: 'opnieuw verbinden…',
    geefVrij: 'Geef vrij', verbind: 'Verbinden',
    flitsAan: 'Flits aan', flitsUit: 'Flits uit', flitsTril: 'Flits + trillen',
    namen: 'Namen', namenTitel: 'Namen van de bronnen',
    annuleer: 'Annuleren', opslaan: 'Opslaan', hint: 'PGM rood, PVW groen',
    bron: 'Bron', taal: 'Taal', sluit: 'Sluiten' },
  en: { eigenNaam: 'English', kode: 'EN',
    verbinden: 'connecting…', verbonden: 'connected', geenVerbinding: 'no connection',
    vrijgegeven: 'released', switcherVrij: 'switcher free', opnieuw: 'reconnecting…',
    geefVrij: 'Release', verbind: 'Connect',
    flitsAan: 'Flash on', flitsUit: 'Flash off', flitsTril: 'Flash + vibrate',
    namen: 'Names', namenTitel: 'Source names',
    annuleer: 'Cancel', opslaan: 'Save', hint: 'PGM red, PVW green',
    bron: 'Source', taal: 'Language', sluit: 'Close' },
  es: { eigenNaam: 'Español', kode: 'ES',
    verbinden: 'conectando…', verbonden: 'conectado', geenVerbinding: 'sin conexión',
    vrijgegeven: 'liberado', switcherVrij: 'switcher libre', opnieuw: 'reconectando…',
    geefVrij: 'Liberar', verbind: 'Conectar',
    flitsAan: 'Flash activado', flitsUit: 'Flash desactivado', flitsTril: 'Flash + vibración',
    namen: 'Nombres', namenTitel: 'Nombres de las fuentes',
    annuleer: 'Cancelar', opslaan: 'Guardar', hint: 'PGM rojo, PVW verde',
    bron: 'Fuente', taal: 'Idioma', sluit: 'Cerrar' },
  fr: { eigenNaam: 'Français', kode: 'FR',
    verbinden: 'connexion…', verbonden: 'connecté', geenVerbinding: 'pas de connexion',
    vrijgegeven: 'libéré', switcherVrij: 'switcher libre', opnieuw: 'reconnexion…',
    geefVrij: 'Libérer', verbind: 'Connecter',
    flitsAan: 'Flash activé', flitsUit: 'Flash désactivé', flitsTril: 'Flash + vibration',
    namen: 'Noms', namenTitel: 'Noms des sources',
    annuleer: 'Annuler', opslaan: 'Enregistrer', hint: 'PGM rouge, PVW vert',
    bron: 'Source', taal: 'Langue', sluit: 'Fermer' },
  de: { eigenNaam: 'Deutsch', kode: 'DE',
    verbinden: 'verbinden…', verbonden: 'verbunden', geenVerbinding: 'keine Verbindung',
    vrijgegeven: 'freigegeben', switcherVrij: 'Switcher frei', opnieuw: 'erneut verbinden…',
    geefVrij: 'Freigeben', verbind: 'Verbinden',
    flitsAan: 'Blitz an', flitsUit: 'Blitz aus', flitsTril: 'Blitz + Vibration',
    namen: 'Namen', namenTitel: 'Quellennamen',
    annuleer: 'Abbrechen', opslaan: 'Speichern', hint: 'PGM rot, PVW grün',
    bron: 'Quelle', taal: 'Sprache', sluit: 'Schließen' },
  pt: { eigenNaam: 'Português', kode: 'PT',
    verbinden: 'a ligar…', verbonden: 'ligado', geenVerbinding: 'sem ligação',
    vrijgegeven: 'libertado', switcherVrij: 'switcher livre', opnieuw: 'a religar…',
    geefVrij: 'Libertar', verbind: 'Ligar',
    flitsAan: 'Flash ligado', flitsUit: 'Flash desligado', flitsTril: 'Flash + vibração',
    namen: 'Nomes', namenTitel: 'Nomes das fontes',
    annuleer: 'Cancelar', opslaan: 'Guardar', hint: 'PGM vermelho, PVW verde',
    bron: 'Fonte', taal: 'Idioma', sluit: 'Fechar' },
  it: { eigenNaam: 'Italiano', kode: 'IT',
    verbinden: 'connessione…', verbonden: 'connesso', geenVerbinding: 'nessuna connessione',
    vrijgegeven: 'rilasciato', switcherVrij: 'switcher libero', opnieuw: 'riconnessione…',
    geefVrij: 'Rilascia', verbind: 'Connetti',
    flitsAan: 'Flash attivo', flitsUit: 'Flash disattivo', flitsTril: 'Flash + vibrazione',
    namen: 'Nomi', namenTitel: 'Nomi delle sorgenti',
    annuleer: 'Annulla', opslaan: 'Salva', hint: 'PGM rosso, PVW verde',
    bron: 'Sorgente', taal: 'Lingua', sluit: 'Chiudi' },
  zh: { eigenNaam: '中文', kode: '中文',
    verbinden: '正在连接…', verbonden: '已连接', geenVerbinding: '无连接',
    vrijgegeven: '已释放', switcherVrij: '切换台空闲', opnieuw: '正在重新连接…',
    geefVrij: '释放', verbind: '连接',
    flitsAan: '闪光开', flitsUit: '闪光关', flitsTril: '闪光 + 震动',
    namen: '名称', namenTitel: '信号源名称',
    annuleer: '取消', opslaan: '保存', hint: 'PGM 红，PVW 绿',
    bron: '信号源', taal: '语言', sluit: '关闭' },
  ja: { eigenNaam: '日本語', kode: 'JA',
    verbinden: '接続中…', verbonden: '接続済み', geenVerbinding: '未接続',
    vrijgegeven: '解放済み', switcherVrij: 'スイッチャー空き', opnieuw: '再接続中…',
    geefVrij: '解放', verbind: '接続',
    flitsAan: 'フラッシュ オン', flitsUit: 'フラッシュ オフ', flitsTril: 'フラッシュ + 振動',
    namen: '名前', namenTitel: 'ソース名',
    annuleer: 'キャンセル', opslaan: '保存', hint: 'PGM 赤、PVW 緑',
    bron: 'ソース', taal: '言語', sluit: '閉じる' },
  ko: { eigenNaam: '한국어', kode: 'KO',
    verbinden: '연결 중…', verbonden: '연결됨', geenVerbinding: '연결 없음',
    vrijgegeven: '해제됨', switcherVrij: '스위처 사용 가능', opnieuw: '다시 연결 중…',
    geefVrij: '해제', verbind: '연결',
    flitsAan: '플래시 켬', flitsUit: '플래시 끔', flitsTril: '플래시 + 진동',
    namen: '이름', namenTitel: '소스 이름',
    annuleer: '취소', opslaan: '저장', hint: 'PGM 빨강, PVW 초록',
    bron: '소스', taal: '언어', sluit: '닫기' },
  ru: { eigenNaam: 'Русский', kode: 'RU',
    verbinden: 'подключение…', verbonden: 'подключено', geenVerbinding: 'нет связи',
    vrijgegeven: 'освобождено', switcherVrij: 'свитчер свободен', opnieuw: 'переподключение…',
    geefVrij: 'Освободить', verbind: 'Подключить',
    flitsAan: 'Вспышка вкл', flitsUit: 'Вспышка выкл', flitsTril: 'Вспышка + вибрация',
    namen: 'Названия', namenTitel: 'Названия источников',
    annuleer: 'Отмена', opslaan: 'Сохранить', hint: 'PGM красный, PVW зелёный',
    bron: 'Источник', taal: 'Язык', sluit: 'Закрыть' },
  ar: { eigenNaam: 'العربية', kode: 'AR', rtl: true,
    verbinden: 'جارٍ الاتصال…', verbonden: 'متصل', geenVerbinding: 'لا يوجد اتصال',
    vrijgegeven: 'تم التحرير', switcherVrij: 'السويتشر حر', opnieuw: 'إعادة الاتصال…',
    geefVrij: 'تحرير', verbind: 'اتصال',
    flitsAan: 'وميض مفعّل', flitsUit: 'وميض متوقف', flitsTril: 'وميض + اهتزاز',
    namen: 'الأسماء', namenTitel: 'أسماء المصادر',
    annuleer: 'إلغاء', opslaan: 'حفظ', hint: 'PGM أحمر، PVW أخضر',
    bron: 'المصدر', taal: 'اللغة', sluit: 'إغلاق' },
  hi: { eigenNaam: 'हिन्दी', kode: 'HI',
    verbinden: 'कनेक्ट हो रहा है…', verbonden: 'कनेक्टेड', geenVerbinding: 'कोई कनेक्शन नहीं',
    vrijgegeven: 'मुक्त', switcherVrij: 'स्विचर खाली', opnieuw: 'फिर से कनेक्ट…',
    geefVrij: 'मुक्त करें', verbind: 'कनेक्ट करें',
    flitsAan: 'फ़्लैश चालू', flitsUit: 'फ़्लैश बंद', flitsTril: 'फ़्लैश + कंपन',
    namen: 'नाम', namenTitel: 'स्रोत के नाम',
    annuleer: 'रद्द करें', opslaan: 'सहेजें', hint: 'PGM लाल, PVW हरा',
    bron: 'स्रोत', taal: 'भाषा', sluit: 'बंद करें' },
  id: { eigenNaam: 'Bahasa Indonesia', kode: 'ID',
    verbinden: 'menghubungkan…', verbonden: 'terhubung', geenVerbinding: 'tidak ada koneksi',
    vrijgegeven: 'dilepas', switcherVrij: 'switcher bebas', opnieuw: 'menghubungkan ulang…',
    geefVrij: 'Lepaskan', verbind: 'Hubungkan',
    flitsAan: 'Flash aktif', flitsUit: 'Flash nonaktif', flitsTril: 'Flash + getar',
    namen: 'Nama', namenTitel: 'Nama sumber',
    annuleer: 'Batal', opslaan: 'Simpan', hint: 'PGM merah, PVW hijau',
    bron: 'Sumber', taal: 'Bahasa', sluit: 'Tutup' }
};

let huidigeTaal = localStorage.getItem('tallyTaal');
if (!huidigeTaal || !TEKST[huidigeTaal]) {
  const systeem = (navigator.language || 'nl').slice(0, 2).toLowerCase();
  huidigeTaal = TEKST[systeem] ? systeem : 'en';
}

function t(sleutel) {
  const bron = TEKST[huidigeTaal] || TEKST.en;
  return bron[sleutel] !== undefined ? bron[sleutel] : (TEKST.nl[sleutel] || '');
}


// Teksten van het toegangsscherm, per taal.
const TOEGANG = {
  pap: { titel: "Akseso nesesario", uitleg: "E tally aki ta protehá. Pidí e persona tras di e PC pa duna bo akseso.",
    vraag: "Pidí akseso", wacht: "Ta spera aprobashon na e PC…", nee: "E PC a nenga e akseso.",
    cod: "E persona tras di e PC ta mira e mesun kódigo." },
  nl: { titel: "Toegang nodig", uitleg: "Deze tally is beveiligd. Vraag de persoon achter de pc om toegang.",
    vraag: "Toegang vragen", wacht: "Wachten op goedkeuring op de pc…", nee: "De pc heeft de toegang geweigerd.",
    cod: "De persoon achter de pc ziet dezelfde code." },
  en: { titel: "Access needed", uitleg: "This tally is protected. Ask the person at the PC for access.",
    vraag: "Request access", wacht: "Waiting for approval on the PC…", nee: "The PC denied access.",
    cod: "The person at the PC sees the same code." },
  es: { titel: "Acceso necesario", uitleg: "Este tally está protegido. Pide acceso a la persona del PC.",
    vraag: "Solicitar acceso", wacht: "Esperando aprobación en el PC…", nee: "El PC ha denegado el acceso.",
    cod: "La persona del PC ve el mismo código." },
  fr: { titel: "Accès requis", uitleg: "Ce tally est protégé. Demandez autorisation à la personne devant le PC.",
    vraag: "Demander accès", wacht: "En attente d'approbation sur le PC…", nee: "Le PC a refusé l'accès.",
    cod: "La personne devant le PC voit le même code." },
  de: { titel: "Zugang nötig", uitleg: "Diese Tally ist geschützt. Fragen Sie die Person am PC.",
    vraag: "Zugang anfragen", wacht: "Warte auf Freigabe am PC…", nee: "Der PC hat den Zugang verweigert.",
    cod: "Die Person am PC sieht denselben Code." },
  pt: { titel: "Acesso necessário", uitleg: "Este tally está protegido. Peça acesso à pessoa no PC.",
    vraag: "Pedir acesso", wacht: "A aguardar aprovação no PC…", nee: "O PC recusou o acesso.",
    cod: "A pessoa no PC vê o mesmo código." },
  it: { titel: "Accesso necessario", uitleg: "Questo tally è protetto. Chiedi accesso alla persona al PC.",
    vraag: "Richiedi accesso", wacht: "In attesa di approvazione sul PC…", nee: "Il PC ha negato accesso.",
    cod: "La persona al PC vede lo stesso codice." },
  zh: { titel: "需要授权", uitleg: "此计时器受保护。请向电脑前的人申请访问。",
    vraag: "申请访问", wacht: "正在等待电脑批准…", nee: "电脑已拒绝访问。",
    cod: "电脑前的人会看到相同的代码。" },
  ja: { titel: "アクセスが必要です", uitleg: "このタリーは保護されています。PCの担当者に許可を求めてください。",
    vraag: "アクセスを要求", wacht: "PCの承認を待っています…", nee: "PCがアクセスを拒否しました。",
    cod: "PCの担当者にも同じコードが表示されます。" },
  ko: { titel: "접근 권한 필요", uitleg: "이 태리는 보호되어 있습니다. PC 담당자에게 접근을 요청하세요.",
    vraag: "접근 요청", wacht: "PC의 승인을 기다리는 중…", nee: "PC가 접근을 거부했습니다.",
    cod: "PC 담당자에게도 같은 코드가 표시됩니다." },
  ru: { titel: "Нужен доступ", uitleg: "Этот талли защищён. Попросите доступ у человека за ПК.",
    vraag: "Запросить доступ", wacht: "Ожидание подтверждения на ПК…", nee: "ПК отклонил доступ.",
    cod: "Человек за ПК видит тот же код." },
  ar: { titel: "الوصول مطلوب", uitleg: "هذا التالي محمي. اطلب الوصول من الشخص أمام الكمبيوتر.",
    vraag: "طلب الوصول", wacht: "في انتظار الموافقة على الكمبيوتر…", nee: "رفض الكمبيوتر الوصول.",
    cod: "الشخص أمام الكمبيوتر يرى نفس الرمز." },
  hi: { titel: "एक्सेस आवश्यक", uitleg: "यह टैली सुरक्षित है। पीसी पर बैठे व्यक्ति से एक्सेस मांगें।",
    vraag: "एक्सेस मांगें", wacht: "पीसी पर स्वीकृति की प्रतीक्षा…", nee: "पीसी ने एक्सेस अस्वीकार किया।",
    cod: "पीसी पर बैठा व्यक्ति वही कोड देखता है।" },
  id: { titel: "Akses diperlukan", uitleg: "Tally ini dilindungi. Minta akses kepada orang di PC.",
    vraag: "Minta akses", wacht: "Menunggu persetujuan di PC…", nee: "PC menolak akses.",
    cod: "Orang di PC melihat kode yang sama." }
};

const langBtn = document.getElementById('lang');
const taalscherm = document.getElementById('taalscherm');
const taallijst = document.getElementById('taallijst');
const taalTitel = document.getElementById('taalTitel');
let laatsteSnap = null;

function bouwTaallijst() {
  taallijst.innerHTML = '';
  Object.keys(TEKST).forEach(code => {
    const b = document.createElement('button');
    b.textContent = TEKST[code].eigenNaam;
    if (code === huidigeTaal) b.classList.add('actief');
    b.addEventListener('click', () => {
      zetTaal(code);
      taalscherm.classList.remove('open');
    });
    taallijst.appendChild(b);
  });
}

function pasTaalToe() {
  document.documentElement.dir = t('rtl') ? 'rtl' : 'ltr';
  langBtn.textContent = TEKST[huidigeTaal].kode;
  namesBtn.textContent = t('namen');
  buzzBtn.textContent = melding ? (kanTrillen ? t('flitsTril') : t('flitsAan')) : t('flitsUit');
  document.getElementById('hint').textContent = t('hint');
  document.querySelector('#editor h2').textContent = t('namenTitel');
  document.getElementById('edcancel').textContent = t('annuleer');
  document.getElementById('edsave').textContent = t('opslaan');
  taalTitel.textContent = t('taal');
  zetToegangStand(toegangStand);
  document.getElementById('taalsluit').textContent = t('sluit');
  bouwTaallijst();
  if (laatsteSnap) apply(laatsteSnap);
}

function zetTaal(code) {
  if (!TEKST[code]) return;
  huidigeTaal = code;
  localStorage.setItem('tallyTaal', code);
  pasTaalToe();
}

langBtn.addEventListener('click', () => taalscherm.classList.add('open'));
document.getElementById('taalsluit').addEventListener('click', () => taalscherm.classList.remove('open'));
const buzzBtn = document.getElementById('buzz');
let labels = [];
let names = {};
let tiles = {};
let prevPgm = null;
// Geen geluid: tijdens opnames en uitzendingen mag er niets piepen.
// Het signaal is een flits over het scherm, plus trillen waar het toestel
// dat toelaat (Android). Een iPhone kan vanuit een webpagina niet trillen.
const kanTrillen = typeof navigator.vibrate === 'function';
let melding = localStorage.getItem('tallyAlert') !== 'off';
const flitsEl = document.getElementById('flits');
const randEl = document.getElementById('rand');
const eigenEl = document.getElementById('eigen');

function meld() {
  if (!melding) return;
  if (kanTrillen) navigator.vibrate([80, 50, 80]);
  flitsEl.classList.add('aan');
  setTimeout(() => flitsEl.classList.remove('aan'), 300);
}

function setMelding(aan) {
  melding = aan;
  localStorage.setItem('tallyAlert', aan ? 'on' : 'off');
  buzzBtn.classList.toggle('active', aan);
  buzzBtn.textContent = aan ? (kanTrillen ? t('flitsTril') : t('flitsAan')) : t('flitsUit');
  if (aan && kanTrillen) navigator.vibrate(30);
}
setMelding(melding);
buzzBtn.addEventListener('click', () => setMelding(!melding));

const namesBtn = document.getElementById('names');
const editor = document.getElementById('editor');
const rows = document.getElementById('rows');

namesBtn.addEventListener('click', () => {
  rows.innerHTML = '';
  const tonen = [1, 2, 3, 4, 5, 6, 7, 8, 5001, 3010, 3020];
  tonen.forEach(id => {
    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = '<span>' + id + '</span><input type="text" maxlength="40">';
    row.querySelector('input').value = naamVan(id);
    row.dataset.id = String(id);
    rows.appendChild(row);
  });
  editor.classList.add('open');
});
document.getElementById('edcancel').addEventListener('click', () => editor.classList.remove('open'));
document.getElementById('edsave').addEventListener('click', async () => {
  const payload = {};
  rows.querySelectorAll('.row').forEach(row => {
    const veld = row.querySelector('input');
    const tekst = veld.value.trim();
    if (tekst) payload[row.dataset.id] = tekst;
  });
  await api('/labels', { method: 'POST', body: JSON.stringify(payload) });
  editor.classList.remove('open');
  const vers = await api('/state').then(r => r.json());
  apply(vers);
});

const releaseBtn = document.getElementById('release');
let paused = false;
releaseBtn.addEventListener('click', async () => {
  const actie = paused ? 'resume' : 'pause';
  await api('/' + actie, { method: 'POST' });
});

// Vaste tegels: de acht ingangen, daarna de extra bronnen.
const INGANGEN = [1, 2, 3, 4, 5, 6, 7, 8];
const EXTRA = [5001, 3010, 3020];   // MultiSource, Media 1, Media 2
let opgebouwd = false;

function tegel(src) {
  const el = document.createElement('div');
  el.className = 'tile';
  el.dataset.src = String(src);
  el.innerHTML = '<div class="name"></div><div class="role"></div>';
  grid.appendChild(el);
  tiles[String(src)] = el;
  return el;
}

function buildTiles() {
  if (opgebouwd) return;
  opgebouwd = true;
  grid.innerHTML = '';
  tiles = {};
  INGANGEN.forEach(tegel);
  EXTRA.forEach(tegel);
}

function naamVan(src) {
  return names[src] || (t('bron') + ' ' + src);
}

function apply(snap) {
  laatsteSnap = snap;
  names = snap.names || {};
  buildTiles();
  const v = snap.values || {};
  dot.classList.toggle('on', !!snap.connected);
  paused = !!snap.paused;
  connlabel.textContent = paused ? t('vrijgegeven') : (snap.connected ? t('verbonden') : t('geenVerbinding'));
  dot.style.background = paused ? '#e08b2b' : '';
  switcherEl.textContent = paused ? t('switcherVrij') : (snap.switcher || '');
  versionEl.textContent = snap.version ? 'v' + snap.version : '';

  releaseBtn.textContent = paused ? t('verbind') : t('geefVrij');
  const heeftPgm = typeof v.pgmIndex === 'number';
  const pgm = String(v.pgmIndex), pvw = String(v.pvwIndex);
  // eigen ingang van dit toestel: rand groen bij preview, rood bij on air
  const eigen = (typeof snap.assigned === 'number') ? String(snap.assigned) : null;
  if (eigen === null || paused) {
    randEl.className = '';
    eigenEl.hidden = true;
    eigenEl.textContent = '';
  } else {
    randEl.className = eigen === pgm ? 'pgm' : (eigen === pvw ? 'pvw' : '');
    eigenEl.textContent = naamVan(eigen);
    eigenEl.hidden = false;
  }

  // een onverwachte bron (bijvoorbeeld kleurbalken) krijgt tijdelijk een eigen tegel
  [pgm, pvw].forEach(src => { if (src !== '0' && !tiles[src]) tegel(src); });

  Object.entries(tiles).forEach(([src, el]) => {
    const isPgm = src === pgm, isPvw = src === pvw;
    el.querySelector('.name').textContent = naamVan(src);
    el.classList.toggle('pgm', isPgm);
    el.classList.toggle('pvw', isPvw);
    el.querySelector('.role').textContent = isPgm ? 'on air' : (isPvw ? 'preview' : '');
  });

  // tijdelijke tegels weer weghalen zodra ze niet meer actief zijn
  Object.keys(tiles).forEach(src => {
    const vast = INGANGEN.includes(Number(src)) || EXTRA.includes(Number(src));
    if (!vast && src !== pgm && src !== pvw) {
      tiles[src].remove();
      delete tiles[src];
    }
  });

  // alleen melden bij een echte wissel, niet bij het opstarten of bij wegvallende data
  if (heeftPgm && prevPgm !== null && prevPgm !== pgm) {
    const el = tiles[pgm];
    if (el) { el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 350); }
    meld();
  }
  if (heeftPgm) prevPgm = pgm;
}

function connect() {
  if (es) { es.close(); es = null; }
  es = new EventSource('/events?token=' + encodeURIComponent(token));
  es.onmessage = e => { try { apply(JSON.parse(e.data)); } catch (_) {} };
  // de pc heeft de verbinding verbroken
  es.addEventListener('weg', () => {
    if (es) { es.close(); es = null; }
    token = '';
    localStorage.removeItem('tallyToken');
    dot.classList.remove('on');
    toonToegang();
  });
  es.onerror = async () => {
    dot.classList.remove('on');
    connlabel.textContent = t('opnieuw');
    if (es) { es.close(); es = null; }
    try {
      const r = await api('/state');
      if (r.status === 401) {          // de pc heeft de verbinding verbroken
        token = '';
        localStorage.removeItem('tallyToken');
        toonToegang();
        return;
      }
    } catch (_) { /* volgende poging */ }
    setTimeout(connect, 2000);
  };
}

async function startTally() {
  if (!token) { toonToegang(); return; }
  try {
    const r = await api('/state');
    if (r.status === 401) {
      token = '';
      localStorage.removeItem('tallyToken');
      toonToegang();
      return;
    }
    if (!r.ok) { toonToegang(); return; }
    apply(await r.json());
    toegangEl.classList.remove('open');
    connect();
  } catch (_) {
    dot.classList.remove('on');
    connlabel.textContent = t('geenVerbinding');
    setTimeout(startTally, 2500);
  }
}

pasTaalToe();
startTally();
</script>
</body>
</html>
"""

HOST_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GoStream Tally</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 20px; background: #0b0b0f; color: #e6e6ee;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
  h1 { font-size: 19px; margin: 0 0 4px; letter-spacing: .02em; }
  h2 { font-size: 15px; margin: 0 0 4px; letter-spacing: .02em; }
  .kop { display: flex; align-items: center; gap: 12px; max-width: 760px; }
  .kop h1 { flex: 1; }
  select { font-family: inherit; font-size: 12px; font-weight: 600; padding: 7px 9px;
    border-radius: 8px; border: 1px solid #2e2e3c; background: #1c1c26; color: #dcdce6; }
  .uitleg { color: #8a8a99; font-size: 13px; margin: 0 0 18px; line-height: 1.5; max-width: 640px; }
  #lijst { display: flex; flex-direction: column; gap: 10px; max-width: 760px; }
  .rij { display: flex; align-items: center; gap: 14px; padding: 14px 16px;
    background: #15151c; border: 1px solid #24242f; border-radius: 11px; flex-wrap: wrap; }
  .rij.wacht { border-color: #a9762a; background: #1d1710; }
  .rij.open { border-color: #2b6b3d; background: #101d14; }
  .rij.weg { opacity: .55; }
  .wie { flex: 1; min-width: 170px; }
  .naam { font-weight: 700; font-size: 15px; }
  .klein { font-size: 12px; color: #8a8a99; margin-top: 3px; }
  .stand { font-size: 11px; text-transform: uppercase; letter-spacing: .1em;
    padding: 4px 8px; border-radius: 6px; background: #22222c; color: #9a9aa8; }
  .stand.wacht { background: #4a3416; color: #f0b45c; }
  .stand.open { background: #163a22; color: #6fd68d; }
  .code { font-family: ui-monospace, Consolas, monospace; font-size: 20px; font-weight: 700;
    letter-spacing: .18em; color: #79c8ff; background: #101820; padding: 6px 12px; border-radius: 8px; }
  button { font-family: inherit; font-size: 13px; font-weight: 600; padding: 9px 15px;
    border-radius: 8px; border: 1px solid #2e2e3c; background: #1c1c26; color: #dcdce6; cursor: pointer; }
  button:hover { background: #262633; }
  button.ja { background: #1f6b38; border-color: #2b8a4a; color: #fff; }
  button.ja:hover { background: #25804a; }
  button.nee { background: #2a1a1a; border-color: #55302f; color: #e39b98; }
  button.nee:hover { background: #351f1f; }
  .leeg { color: #6d6d7c; font-size: 14px; padding: 22px; text-align: center;
    border: 1px dashed #2a2a36; border-radius: 11px; }
  .blok { margin-top: 26px; padding: 16px; max-width: 760px; background: #12121a;
    border: 1px solid #24242f; border-radius: 11px; }
  .veld { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 10px; }
  .veld input { flex: 1; min-width: 180px; }
  select.ingang { background: #1c1c26; color: #dcdce6; border: 1px solid #2e2e3c;
    border-radius: 8px; padding: 8px 10px; max-width: 150px; }
  input { font-family: ui-monospace, Consolas, monospace; font-size: 14px; padding: 9px 11px;
    border-radius: 8px; border: 1px solid #2e2e3c; background: #0b0b0f; color: #e6e6ee; }
  #adres { flex: 1; min-width: 170px; }
  #sport { width: 95px; }
  .voet { margin-top: 18px; color: #5f5f6e; font-size: 12px; line-height: 1.6; }
  a { color: #79c8ff; }
</style>
</head>
<body>
<div class="kop">
  <h1 id="titel"></h1>
  <select id="talen"></select>
</div>
<p class="uitleg" id="uitleg"></p>

<div class="blok">
  <h2 id="adresKop"></h2>
  <p class="uitleg" id="adresUitleg" style="margin-bottom:0"></p>
  <div class="veld">
    <input id="adresWaarde" readonly>
    <button id="kopieer"></button>
  </div>
</div>

<div id="lijst"><div class="leeg">...</div></div>

<div class="blok">
  <h2 id="skop"></h2>
  <p class="uitleg" id="suitleg" style="margin-bottom:0"></p>
  <div class="veld">
    <input id="adres" placeholder="192.168.1.232" autocomplete="off">
    <input id="sport" type="number" value="19010">
    <button id="opslaan"></button>
  </div>
  <div class="klein" id="sstand" style="margin-top:8px"></div>
</div>

<p class="voet"><a href="/instructions" id="instr" target="_blank"></a></p>

<script>
const T = {
 pap: { titel:"Akseso na e tally",
  uitleg:"Solo e aparatonan ku bo ta permití aki por habri e página. E kódigo ku ta para na un aparato mester ta igual na esun ku e aparato mes ta mustra.",
  geen:"Ainda no tin ningun aparato ku a pidi akseso.", onbekend:"Aparato deskonosí",
  nu:"abes", gezien:"mirá", vraagt:"ta pidi akseso", verbonden:"konectá", geweigerd:"nengá",
  toestaan:"Konektá", weigeren:"Nengá", verbreken:"Kòrta konekshon",
  skop:"Direkshon di e switcher",
  suitleg:"E bridge ta buska e switcher su mes. Si e no hañ'é, skirbi e direkshon aki.",
  opslaan:"Warda i konektá", instr:"Habri e instrukshonnan",
  foutPc:"E página aki ta funshoná solo riba e PC kaminda e bridge ta kore. Habri'é via http://127.0.0.1.",
  foutBridge:"Sin konekshon ku e bridge. E bentana ta kore ainda?" },
 nl: { titel:"Toegang tot de tally",
  uitleg:"Alleen toestellen die je hier toelaat kunnen de pagina openen. De code bij een toestel moet hetzelfde zijn als wat dat toestel zelf toont.",
  geen:"Nog geen toestel heeft toegang gevraagd.", onbekend:"Onbekend toestel",
  nu:"net", gezien:"gezien", vraagt:"vraagt toegang", verbonden:"verbonden", geweigerd:"geweigerd",
  toestaan:"Connect", weigeren:"Weigeren", verbreken:"Verbinding verbreken",
  skop:"Adres van de switcher",
  suitleg:"De bridge zoekt de switcher zelf. Vindt hij hem niet, vul dan hier het adres in.",
  opslaan:"Opslaan en verbinden", instr:"Instructies openen",
  foutPc:"Deze pagina werkt alleen op de pc waarop de bridge draait. Open hem daar via http://127.0.0.1.",
  foutBridge:"Geen verbinding met de bridge. Draait het venster nog?" },
 en: { titel:"Tally access",
  uitleg:"Only devices you allow here can open the page. The code for a device must match what that device shows.",
  geen:"No device has asked for access yet.", onbekend:"Unknown device",
  nu:"just now", gezien:"seen", vraagt:"asking for access", verbonden:"connected", geweigerd:"denied",
  toestaan:"Connect", weigeren:"Deny", verbreken:"Disconnect",
  skop:"Switcher address",
  suitleg:"The bridge looks for the switcher itself. If it cannot find it, enter the address here.",
  opslaan:"Save and connect", instr:"Open the instructions",
  foutPc:"This page only works on the PC running the bridge. Open it there via http://127.0.0.1.",
  foutBridge:"No connection to the bridge. Is the window still running?" },
 es: { titel:"Acceso al tally",
  uitleg:"Solo los dispositivos que permitas aquí pueden abrir la página. El código de un dispositivo debe coincidir con el que muestra ese dispositivo.",
  geen:"Ningún dispositivo ha pedido acceso todavía.", onbekend:"Dispositivo desconocido",
  nu:"ahora mismo", gezien:"visto", vraagt:"pide acceso", verbonden:"conectado", geweigerd:"denegado",
  toestaan:"Conectar", weigeren:"Denegar", verbreken:"Desconectar",
  skop:"Dirección del switcher",
  suitleg:"El puente busca el switcher por sí mismo. Si no lo encuentra, escribe la dirección aquí.",
  opslaan:"Guardar y conectar", instr:"Abrir las instrucciones",
  foutPc:"Esta página solo funciona en el PC que ejecuta el puente. Ábrela allí con http://127.0.0.1.",
  foutBridge:"Sin conexión con el puente. ¿La ventana sigue abierta?" },
 fr: { titel:"Accès au tally",
  uitleg:"Seuls les appareils que vous autorisez ici peuvent ouvrir la page. Le code d'un appareil doit correspondre à celui qu'il affiche.",
  geen:"Aucun appareil n'a encore demandé l'accès.", onbekend:"Appareil inconnu",
  nu:"à l'instant", gezien:"vu", vraagt:"demande l'accès", verbonden:"connecté", geweigerd:"refusé",
  toestaan:"Connecter", weigeren:"Refuser", verbreken:"Déconnecter",
  skop:"Adresse du switcher",
  suitleg:"Le pont cherche le switcher lui-même. S'il ne le trouve pas, saisissez l'adresse ici.",
  opslaan:"Enregistrer et connecter", instr:"Ouvrir les instructions",
  foutPc:"Cette page ne fonctionne que sur le PC qui exécute le pont. Ouvrez-la là via http://127.0.0.1.",
  foutBridge:"Pas de connexion au pont. La fenêtre est-elle toujours ouverte ?" },
 de: { titel:"Zugang zur Tally",
  uitleg:"Nur Geräte, die Sie hier zulassen, können die Seite öffnen. Der Code eines Geräts muss mit dem übereinstimmen, das Gerät selbst zeigt.",
  geen:"Noch kein Gerät hat Zugang angefragt.", onbekend:"Unbekanntes Gerät",
  nu:"gerade eben", gezien:"gesehen", vraagt:"fragt Zugang an", verbonden:"verbunden", geweigerd:"abgelehnt",
  toestaan:"Verbinden", weigeren:"Ablehnen", verbreken:"Trennen",
  skop:"Adresse des Switchers",
  suitleg:"Die Bridge sucht den Switcher selbst. Findet sie ihn nicht, tragen Sie die Adresse hier ein.",
  opslaan:"Speichern und verbinden", instr:"Anleitung öffnen",
  foutPc:"Diese Seite funktioniert nur auf dem PC, auf dem die Bridge läuft. Öffnen Sie sie dort über http://127.0.0.1.",
  foutBridge:"Keine Verbindung zur Bridge. Läuft das Fenster noch?" },
 pt: { titel:"Acesso ao tally",
  uitleg:"Só os dispositivos que autorizar aqui podem abrir a página. O código de um dispositivo tem de ser igual ao que ele mostra.",
  geen:"Nenhum dispositivo pediu acesso ainda.", onbekend:"Dispositivo desconhecido",
  nu:"agora", gezien:"visto", vraagt:"a pedir acesso", verbonden:"ligado", geweigerd:"recusado",
  toestaan:"Ligar", weigeren:"Recusar", verbreken:"Desligar",
  skop:"Endereço do switcher",
  suitleg:"A ponte procura o switcher sozinha. Se não o encontrar, introduza o endereço aqui.",
  opslaan:"Guardar e ligar", instr:"Abrir as instruções",
  foutPc:"Esta página só funciona no PC que executa a ponte. Abra-a aí através de http://127.0.0.1.",
  foutBridge:"Sem ligação à ponte. A janela ainda está aberta?" },
 it: { titel:"Accesso al tally",
  uitleg:"Solo i dispositivi che autorizzi qui possono aprire la pagina. Il codice di un dispositivo deve corrispondere a quello che mostra.",
  geen:"Nessun dispositivo ha ancora chiesto accesso.", onbekend:"Dispositivo sconosciuto",
  nu:"adesso", gezien:"visto", vraagt:"chiede accesso", verbonden:"connesso", geweigerd:"negato",
  toestaan:"Connetti", weigeren:"Nega", verbreken:"Disconnetti",
  skop:"Indirizzo dello switcher",
  suitleg:"Il bridge cerca lo switcher da solo. Se non lo trova, inserisci qui l'indirizzo.",
  opslaan:"Salva e connetti", instr:"Apri le istruzioni",
  foutPc:"Questa pagina funziona solo sul PC che esegue il bridge. Aprila lì tramite http://127.0.0.1.",
  foutBridge:"Nessuna connessione al bridge. La finestra è ancora aperta?" },
 zh: { titel:"计时器访问",
  uitleg:"只有你在此允许的设备才能打开页面。设备上的代码必须与该设备显示的代码一致。",
  geen:"还没有设备申请访问。", onbekend:"未知设备",
  nu:"刚刚", gezien:"看到", vraagt:"申请访问", verbonden:"已连接", geweigerd:"已拒绝",
  toestaan:"连接", weigeren:"拒绝", verbreken:"断开连接",
  skop:"切换台地址",
  suitleg:"桥接会自动查找切换台。如果找不到，请在此输入地址。",
  opslaan:"保存并连接", instr:"打开说明",
  foutPc:"此页面只能在运行桥接的电脑上使用。请在那台电脑上通过 http://127.0.0.1 打开。",
  foutBridge:"无法连接桥接。窗口还在运行吗？" },
 ja: { titel:"タリーへのアクセス",
  uitleg:"ここで許可した端末だけがページを開けます。端末のコードは、その端末に表示されるコードと同じでなければなりません。",
  geen:"まだアクセスを要求した端末はありません。", onbekend:"不明な端末",
  nu:"たった今", gezien:"確認", vraagt:"アクセス要求中", verbonden:"接続済み", geweigerd:"拒否",
  toestaan:"接続", weigeren:"拒否する", verbreken:"切断",
  skop:"スイッチャーのアドレス",
  suitleg:"ブリッジはスイッチャーを自動で探します。見つからない場合はここにアドレスを入力してください。",
  opslaan:"保存して接続", instr:"手順を開く",
  foutPc:"このページはブリッジを実行しているPCでのみ動作します。そのPCで http://127.0.0.1 から開いてください。",
  foutBridge:"ブリッジに接続できません。ウィンドウはまだ開いていますか？" },
 ko: { titel:"탈리 접근",
  uitleg:"여기서 허용한 기기만 페이지를 열 수 있습니다. 기기의 코드는 그 기기에 표시되는 코드와 같아야 합니다.",
  geen:"아직 접근을 요청한 기기가 없습니다.", onbekend:"알 수 없는 기기",
  nu:"방금", gezien:"확인", vraagt:"접근 요청 중", verbonden:"연결됨", geweigerd:"거부됨",
  toestaan:"연결", weigeren:"거부", verbreken:"연결 해제",
  skop:"스위처 주소",
  suitleg:"브리지가 스위처를 자동으로 찾습니다. 찾지 못하면 여기에 주소를 입력하세요.",
  opslaan:"저장 후 연결", instr:"설명 열기",
  foutPc:"이 페이지는 브리지가 실행 중인 PC에서만 작동합니다. 그 PC에서 http://127.0.0.1 로 여세요.",
  foutBridge:"브리지에 연결할 수 없습니다. 창이 아직 열려 있나요?" },
 ru: { titel:"Доступ к талли",
  uitleg:"Открыть страницу могут только устройства, разрешённые здесь. Код устройства должен совпадать с тем, что показывает само устройство.",
  geen:"Пока ни одно устройство не запросило доступ.", onbekend:"Неизвестное устройство",
  nu:"только что", gezien:"видели", vraagt:"запрашивает доступ", verbonden:"подключено", geweigerd:"отклонено",
  toestaan:"Подключить", weigeren:"Отклонить", verbreken:"Отключить",
  skop:"Адрес свитчера",
  suitleg:"Мост ищет свитчер сам. Если не находит, введите адрес здесь.",
  opslaan:"Сохранить и подключить", instr:"Открыть инструкцию",
  foutPc:"Эта страница работает только на ПК, где запущен мост. Откройте её там через http://127.0.0.1.",
  foutBridge:"Нет связи с мостом. Окно ещё открыто?" },
 ar: { titel:"الوصول إلى العدّاد",
  uitleg:"لا يمكن فتح الصفحة إلا للأجهزة التي تسمح لها هنا. يجب أن يطابق رمز الجهاز ما يعرضه الجهاز نفسه.",
  geen:"لم يطلب أي جهاز الوصول بعد.", onbekend:"جهاز غير معروف",
  nu:"الآن", gezien:"شوهد", vraagt:"يطلب الوصول", verbonden:"متصل", geweigerd:"مرفوض",
  toestaan:"اتصال", weigeren:"رفض", verbreken:"قطع الاتصال",
  skop:"عنوان المبدّل",
  suitleg:"يبحث الجسر عن المبدّل بنفسه. إن لم يجده، أدخل العنوان هنا.",
  opslaan:"حفظ واتصال", instr:"فتح التعليمات",
  foutPc:"تعمل هذه الصفحة فقط على الحاسوب الذي يشغّل الجسر. افتحها هناك عبر http://127.0.0.1.",
  foutBridge:"لا اتصال بالجسر. هل النافذة ما زالت مفتوحة؟" },
 hi: { titel:"टैली एक्सेस",
  uitleg:"केवल वे डिवाइस पेज खोल सकते हैं जिन्हें आप यहाँ अनुमति देते हैं। डिवाइस का कोड वही होना चाहिए जो वह डिवाइस दिखाता है।",
  geen:"अभी किसी डिवाइस ने एक्सेस नहीं माँगा।", onbekend:"अज्ञात डिवाइस",
  nu:"अभी", gezien:"देखा", vraagt:"एक्सेस माँग रहा है", verbonden:"जुड़ा हुआ", geweigerd:"अस्वीकृत",
  toestaan:"कनेक्ट", weigeren:"अस्वीकार", verbreken:"डिस्कनेक्ट",
  skop:"स्विचर का पता",
  suitleg:"ब्रिज स्विचर को स्वयं खोजता है। न मिले तो पता यहाँ भरें।",
  opslaan:"सेव करें और जोड़ें", instr:"निर्देश खोलें",
  foutPc:"यह पेज केवल उसी पीसी पर काम करता है जहाँ ब्रिज चल रहा है। वहाँ http://127.0.0.1 से खोलें।",
  foutBridge:"ब्रिज से कनेक्शन नहीं। क्या विंडो अभी भी खुली है?" },
 id: { titel:"Akses tally",
  uitleg:"Hanya perangkat yang Anda izinkan di sini yang dapat membuka halaman. Kode perangkat harus sama dengan yang ditampilkan perangkat itu.",
  geen:"Belum ada perangkat yang meminta akses.", onbekend:"Perangkat tidak dikenal",
  nu:"baru saja", gezien:"terlihat", vraagt:"meminta akses", verbonden:"terhubung", geweigerd:"ditolak",
  toestaan:"Hubungkan", weigeren:"Tolak", verbreken:"Putuskan",
  skop:"Alamat switcher",
  suitleg:"Bridge mencari switcher sendiri. Jika tidak menemukannya, masukkan alamat di sini.",
  opslaan:"Simpan dan hubungkan", instr:"Buka petunjuk",
  foutPc:"Halaman ini hanya berfungsi di PC yang menjalankan bridge. Buka di sana melalui http://127.0.0.1.",
  foutBridge:"Tidak ada koneksi ke bridge. Apakah jendelanya masih terbuka?" }
};

const talen = [
  ['pap','Papiamentu'],['nl','Nederlands'],['en','English'],['es','Español'],['fr','Français'],
  ['de','Deutsch'],['pt','Português'],['it','Italiano'],['zh','中文'],['ja','日本語'],
  ['ko','한국어'],['ru','Русский'],['ar','العربية'],['hi','हिन्दी'],['id','Bahasa Indonesia']
];

const E = {
 pap: { adresKop:"Direkshon pa e telefòn", adresUitleg:"Habri e direkshon aki riba e telefon òf tablet.",
  kopieer:"Kopia", ingang:"Entrada" },
 nl: { adresKop:"Adres voor de telefoon", adresUitleg:"Open dit adres op de telefoon of tablet.",
  kopieer:"Kopiëren", ingang:"Ingang" },
 en: { adresKop:"Address for the phone", adresUitleg:"Open this address on the phone or tablet.",
  kopieer:"Copy", ingang:"Input" },
 es: { adresKop:"Dirección para el teléfono", adresUitleg:"Abre esta dirección en el teléfono o la tableta.",
  kopieer:"Copiar", ingang:"Entrada" },
 fr: { adresKop:"Adresse pour le téléphone", adresUitleg:"Ouvrez cette adresse sur le téléphone ou la tablette.",
  kopieer:"Copier", ingang:"Entrée" },
 de: { adresKop:"Adresse für das Telefon", adresUitleg:"Öffnen Sie diese Adresse auf dem Telefon oder Tablet.",
  kopieer:"Kopieren", ingang:"Eingang" },
 pt: { adresKop:"Endereço para o telefone", adresUitleg:"Abra este endereço no telefone ou tablet.",
  kopieer:"Copiar", ingang:"Entrada" },
 it: { adresKop:"Indirizzo per il telefono", adresUitleg:"Apri questo indirizzo sul telefono o tablet.",
  kopieer:"Copia", ingang:"Ingresso" },
 zh: { adresKop:"手机地址", adresUitleg:"在手机或平板上打开此地址。", kopieer:"复制", ingang:"输入" },
 ja: { adresKop:"スマホ用アドレス", adresUitleg:"このアドレスをスマホまたはタブレットで開きます。",
  kopieer:"コピー", ingang:"入力" },
 ko: { adresKop:"휴대폰 주소", adresUitleg:"이 주소를 휴대폰이나 태블릿에서 여세요.",
  kopieer:"복사", ingang:"입력" },
 ru: { adresKop:"Адрес для телефона", adresUitleg:"Откройте этот адрес на телефоне или планшете.",
  kopieer:"Копировать", ingang:"Вход" },
 ar: { adresKop:"عنوان الهاتف", adresUitleg:"افتح هذا العنوان على الهاتف أو الجهاز اللوحي.",
  kopieer:"نسخ", ingang:"مدخل" },
 hi: { adresKop:"फ़ोन का पता", adresUitleg:"इस पते को फ़ोन या टैबलेट पर खोलें।",
  kopieer:"कॉपी", ingang:"इनपुट" },
 id: { adresKop:"Alamat untuk ponsel", adresUitleg:"Buka alamat ini di ponsel atau tablet.",
  kopieer:"Salin", ingang:"Masukan" },
};
function tt2(sleutel) {
  const bron = E[taal] || E.en;
  return bron[sleutel] || E.en[sleutel] || '';
}
const lijst = document.getElementById('lijst');
const keuze = document.getElementById('talen');
const adresVeld = document.getElementById('adres');
const poortVeld = document.getElementById('sport');
let bezig = false;
let laatsteInhoud = null;
let laatsteIngangen = [];
let taal = localStorage.getItem('hostTaal');
if (!taal || !T[taal]) {
  const systeem = (navigator.language || 'en').slice(0, 2).toLowerCase();
  taal = T[systeem] ? systeem : 'en';
}
function tt(s) { return (T[taal] && T[taal][s]) || T.en[s]; }

function tijd(sec) {
  if (sec < 60) return tt('nu');
  if (sec < 3600) return Math.floor(sec / 60) + ' min';
  if (sec < 86400) return Math.floor(sec / 3600) + ' h';
  return Math.floor(sec / 86400) + ' d';
}

function pasTaalToe() {
  document.getElementById('titel').textContent = tt('titel');
  document.getElementById('uitleg').textContent = tt('uitleg');
  document.getElementById('skop').textContent = tt('skop');
  document.getElementById('suitleg').textContent = tt('suitleg');
  document.getElementById('opslaan').textContent = tt('opslaan');
  document.getElementById('instr').textContent = tt('instr');
  document.getElementById('adresKop').textContent = tt2('adresKop');
  document.getElementById('adresUitleg').textContent = tt2('adresUitleg');
  document.getElementById('kopieer').textContent = tt2('kopieer');
  document.documentElement.lang = taal;
  document.documentElement.dir = (taal === 'ar') ? 'rtl' : 'ltr';
  laatsteInhoud = null;
  ververs();
}

talen.forEach(([code, naam]) => {
  const o = document.createElement('option');
  o.value = code; o.textContent = naam;
  if (code === taal) o.selected = true;
  keuze.append(o);
});
keuze.onchange = () => {
  taal = keuze.value;
  localStorage.setItem('hostTaal', taal);
  pasTaalToe();
};

async function vraag(pad, device, extra) {
  bezig = true;
  const body = Object.assign({ device: device }, extra || {});
  await fetch(pad, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) });
  bezig = false;
  laatsteInhoud = null;
  ververs();
}

function rij(d) {
  const el = document.createElement('div');
  el.className = 'rij' + (d.status === 'wachtend' ? ' wacht'
    : d.status === 'toegestaan' ? ' open' : ' weg');

  const wie = document.createElement('div');
  wie.className = 'wie';
  const naam = document.createElement('div');
  naam.className = 'naam';
  naam.textContent = d.naam || tt('onbekend');
  const klein = document.createElement('div');
  klein.className = 'klein';
  klein.textContent = (d.ip || '?') + '  ·  ' + tt('gezien') + ' ' + tijd(d.seconden);
  wie.append(naam, klein);

  const stand = document.createElement('span');
  stand.className = 'stand' + (d.status === 'wachtend' ? ' wacht'
    : d.status === 'toegestaan' ? ' open' : '');
  stand.textContent = d.status === 'wachtend' ? tt('vraagt')
    : d.status === 'toegestaan' ? tt('verbonden') : tt('geweigerd');

  el.append(wie, stand);

  if (d.status === 'toegestaan' && d.code) {
    const code = document.createElement('span');
    code.className = 'code';
    code.textContent = d.code;
    el.append(code);
  }

  if (d.status === 'toegestaan') {
    const sel = document.createElement('select');
    sel.className = 'ingang';
    sel.title = tt2('ingang');
    const leeg = document.createElement('option');
    leeg.value = '0';
    leeg.textContent = '\u2014';
    sel.append(leeg);
    laatsteIngangen.forEach(i => {
      const o = document.createElement('option');
      o.value = String(i.nr);
      o.textContent = i.naam || String(i.nr);
      sel.append(o);
    });
    sel.value = d.input ? String(d.input) : '0';
    sel.onchange = () => vraag('/host/assign', d.device, { input: sel.value });
    el.append(sel);

    const uit = document.createElement('button');
    uit.className = 'nee';
    uit.textContent = tt('verbreken');
    uit.onclick = () => vraag('/host/revoke', d.device);
    el.append(uit);
  } else {
    const ja = document.createElement('button');
    ja.className = 'ja';
    ja.textContent = tt('toestaan');
    ja.onclick = () => vraag('/host/allow', d.device);
    el.append(ja);
    if (d.status === 'wachtend') {
      const nee = document.createElement('button');
      nee.className = 'nee';
      nee.textContent = tt('weigeren');
      nee.onclick = () => vraag('/host/deny', d.device);
      el.append(nee);
    }
  }
  return el;
}

function toonMelding(tekst) {
  laatsteInhoud = '';
  lijst.textContent = '';
  const m = document.createElement('div');
  m.className = 'leeg';
  m.textContent = tekst;
  lijst.append(m);
}

async function ververs() {
  if (bezig) return;
  try {
    const antwoord = await fetch('/host/data', { cache: 'no-store' });
    if (antwoord.status === 403) { toonMelding(tt('foutPc')); return; }
    if (!antwoord.ok) { toonMelding('HTTP ' + antwoord.status); return; }
    const data = await antwoord.json();
    const apparaten = data.devices || [];
    if (data.address) document.getElementById('adresWaarde').value = data.address;
    if (data.inputs) laatsteIngangen = data.inputs;
    const vingerafdruk = JSON.stringify(apparaten.map(d => [d.device, d.status, d.code, d.naam]))
      + taal;
    if (laatsteInhoud !== null && vingerafdruk === laatsteInhoud) return;
    laatsteInhoud = vingerafdruk;
    lijst.textContent = '';
    if (!apparaten.length) {
      const leeg = document.createElement('div');
      leeg.className = 'leeg';
      leeg.textContent = tt('geen');
      lijst.append(leeg);
      return;
    }
    apparaten.forEach(d => lijst.append(rij(d)));
  } catch (fout) {
    toonMelding(tt('foutBridge'));
  }
}

async function laadAdres() {
  try {
    const r = await fetch('/host/switcher', { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    if (document.activeElement !== adresVeld) adresVeld.value = d.host || '';
    if (document.activeElement !== poortVeld) poortVeld.value = d.port || 19010;
    document.getElementById('sstand').textContent =
      (d.connected ? tt('verbonden') : tt('geweigerd')) + ': ' + (d.status || d.host || '');
  } catch (fout) { /* stil */ }
}

document.getElementById('opslaan').onclick = async () => {
  const knop = document.getElementById('opslaan');
  const stand = document.getElementById('sstand');
  knop.disabled = true;
  stand.textContent = '...';
  try {
    const r = await fetch('/host/switcher', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: adresVeld.value.trim(), port: Number(poortVeld.value) || 19010 }) });
    if (!r.ok) { stand.textContent = 'HTTP ' + r.status; return; }
    const d = await r.json();
    stand.textContent = tt('verbonden') + ': ' + d.host + ':' + d.port;
  } catch (fout) {
    stand.textContent = tt('foutBridge');
  } finally {
    knop.disabled = false;
  }
};

document.getElementById('kopieer').onclick = () => {
  const veld = document.getElementById('adresWaarde');
  veld.select();
  try { navigator.clipboard.writeText(veld.value); } catch (fout) {
    try { document.execCommand('copy'); } catch (fout2) {}
  }
};

pasTaalToe();
laadAdres();
setInterval(ververs, 2000);
setInterval(laadAdres, 5000);
</script>
</body>
</html>
"""


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """Een weggevallen telefoon of browser is normaal, geen programmeerfout."""
        fout = sys.exc_info()[1]
        if isinstance(fout, (ConnectionError, TimeoutError, OSError)):
            return
        super().handle_error(request, client_address)


EIGEN_ADRESSEN: set = set()


class Handler(BaseHTTPRequestHandler):
    state: TallyState = None  # type: ignore
    access: Access = None  # type: ignore

    def log_message(self, *args) -> None:  # stil
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------------- toegang ----------------
    def _van_pc(self) -> bool:
        """Alleen de pc waarop de bridge draait mag toestellen toelaten."""
        adres = self.client_address[0] if self.client_address else ""
        adres = adres.split("%")[0]
        if adres.startswith("::ffff:"):          # IPv4 in IPv6-vorm
            adres = adres[7:]
        if adres in ("127.0.0.1", "::1"):
            return True
        # Windows meldt de pc zelf soms met het LAN-adres. Dat tellen we mee,
        # maar alleen als het verzoek ook echt aan datzelfde adres gericht was.
        return adres in EIGEN_ADRESSEN

    def _token(self) -> str:
        tok = self.headers.get("X-Tally-Token") or ""
        if not tok:
            query = urllib.parse.urlparse(self.path).query
            tok = urllib.parse.parse_qs(query).get("token", [""])[0]
        return tok.strip()

    def _mag(self) -> bool:
        return bool(self.access and self.access.check(self._token()))

    def _body(self) -> dict:
        lengte = int(self.headers.get("Content-Length") or 0)
        if lengte <= 0:
            return {}
        if lengte > 4096:                      # geen grote brokken aannemen
            raise ValueError("verzoek te groot")
        return json.loads(self.rfile.read(lengte).decode("utf-8"))

    def _geen_toegang(self) -> None:
        self._send(401, b'{"error":"geen toegang"}', "application/json")

    def do_POST(self) -> None:
        pad = urllib.parse.urlparse(self.path).path
        try:
            data = self._body()
        except (ValueError, OSError):
            self._send(400, b"invalid data", "text/plain")
            return

        # een toestel vraagt toegang: dit mag zonder token, anders komt het nooit binnen
        if pad == "/access/request":
            dev = str(data.get("device") or "").strip()[:64]
            naam = str(data.get("naam") or "").strip()[:40]
            if not dev:
                self._send(400, b"geen device", "text/plain")
                return
            ip = self.client_address[0] if self.client_address else ""
            self._send(200, json.dumps(self.access.verzoek(dev, naam, ip)).encode(),
                       "application/json")
            return

        # het adres van de switcher instellen vanaf de pc
        if pad == "/host/switcher":
            if not self._van_pc():
                self._send(403, b"this PC only", "text/plain")
                return
            host = str(data.get("host") or "").strip()
            try:
                port = int(data.get("port") or SWITCHER_PORT)
            except (TypeError, ValueError):
                port = SWITCHER_PORT
            if not host:
                self._send(400, b"geen adres", "text/plain")
                return
            save_switcher(host, port)
            self.doel.host = host
            self.doel.port = port
            self.doel.found_differently = False
            self.doel.last_scan = 0.0
            self.state.configured_host = host
            self.state.doel_teller += 1
            self._send(200, json.dumps({"ok": True, "host": host, "port": port}).encode(),
                       "application/json")
            return

        # een toestel aan een ingang koppelen
        if pad == "/host/assign":
            if not self._van_pc():
                self._send(403, b"this PC only", "text/plain")
                return
            dev = str(data.get("device") or "").strip()
            antwoord = self.access.toewijzen(dev, data.get("input"))
            # de telefoon moet de nieuwe ingang meteen zien, niet pas bij
            # de volgende camerawissel
            self.state.push_now()
            self._send(200, json.dumps(antwoord).encode(), "application/json")
            return

        # de drie knoppen van de pc
        if pad in ("/host/allow", "/host/deny", "/host/revoke"):
            if not self._van_pc():
                self._send(403, b"this PC only", "text/plain")
                return
            dev = str(data.get("device") or "").strip()
            if pad == "/host/allow":
                antwoord = self.access.sta_toe(dev)
            else:
                antwoord = self.access.weiger(dev)
            self._send(200, json.dumps(antwoord).encode(), "application/json")
            return

        if not self._mag():
            self._geen_toegang()
            return

        if pad == "/pause":
            self.state.set_paused(True)
            self._send(200, json.dumps(self.state.snapshot()).encode(), "application/json")
        elif pad == "/resume":
            self.state.set_paused(False)
            self._send(200, json.dumps(self.state.snapshot()).encode(), "application/json")
        elif pad in ("/labels", "/names"):
            try:
                schoon: dict = {}
                for sleutel, waarde in (data or {}).items():
                    try:
                        nummer = int(sleutel)
                    except (TypeError, ValueError):
                        continue
                    tekst = str(waarde).strip()[:40]
                    if tekst:
                        schoon[nummer] = tekst
                self.state.set_names(schoon)
                save_names(self.state.names)
                self._send(200, json.dumps(self.state.snapshot()).encode(), "application/json")
            except (ValueError, OSError):
                self._send(400, b"invalid data", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_GET(self) -> None:
        ontleed = urllib.parse.urlparse(self.path)
        pad = ontleed.path
        query = urllib.parse.parse_qs(ontleed.query)

        if pad in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if pad in ("/host", "/host/"):
            if not self._van_pc():
                self._send(403, b"this PC only", "text/plain")
                return
            self._send(200, HOST_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if pad == "/host/switcher":
            if not self._van_pc():
                self._send(403, b"this PC only", "text/plain")
                return
            self._send(200, json.dumps({
                "host": self.doel.host,
                "port": self.doel.port,
                "connected": self.state.connected,
                "status": self.state.switcher,
            }).encode(), "application/json")
            return

        if pad == "/host/data":
            if not self._van_pc():
                self._send(403, b"this PC only", "text/plain")
                return
            gegevens = self.access.lijst()
            poort = self.server.server_address[1]
            gegevens["address"] = f"http://{local_ip(self.doel.host)}:{poort}"
            namen = self.state.snapshot().get("names") or {}
            ingangen = []
            for sleutel, waarde in namen.items():
                try:
                    nummer = int(sleutel)
                except (TypeError, ValueError):
                    continue
                # 0 is de lege stand. Sleutels (3011, 3021) en de PGM/PVW-
                # doorlussen (10010, 10011) zijn geen ingang om aan te wijzen.
                if nummer > 0 and nummer not in (3011, 3021, 10010, 10011):
                    ingangen.append({"nr": nummer, "naam": waarde})
            ingangen.sort(key=lambda x: x["nr"])
            gegevens["inputs"] = ingangen
            self._send(200, json.dumps(gegevens).encode(), "application/json")
            return

        if pad == "/access/poll":
            dev = (query.get("device", [""])[0] or "").strip()
            self._send(200, json.dumps(self.access.status(dev)).encode(), "application/json")
            return

        if pad == "/health":
            self._send(200, b"ok", "text/plain")
            return

        # de instructies mogen door iedereen gelezen worden: het is uitleg,
        # geen status van de uitzending
        if pad in ("/instructions", "/instructions.html", "/instructies"):
            self._send(200, instructies_html().encode("utf-8"), "text/html; charset=utf-8")
            return

        if not self._mag():
            self._geen_toegang()
            return

        if pad == "/state":
            snap = self.state.snapshot()
            snap["assigned"] = self.access.input_van(self._token())
            body = json.dumps(snap).encode("utf-8")
            self._send(200, body, "application/json")
        elif pad == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = self.state.subscribe()
            sleutel = self._token()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                eerste = self.state.snapshot()
                eerste["assigned"] = self.access.input_van(sleutel)
                self.wfile.write(b"data: " + json.dumps(eerste).encode() + b"\n\n")
                self.wfile.flush()
                while True:
                    try:
                        snap = q.get(timeout=2)
                        snap["assigned"] = self.access.input_van(sleutel)
                        self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    # de pc kan de verbinding op elk moment verbreken; de
                    # lopende stream moet dan meteen stoppen, niet pas bij
                    # een nieuwe pagina.
                    if not self.access.check(sleutel):
                        self.wfile.write(b"event: weg\ndata: {}\n\n")
                        self.wfile.flush()
                        break
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.state.unsubscribe(q)
        else:
            self._send(404, b"not found", "text/plain")


def local_ip(target: str = "") -> str:
    """Het eigen LAN-adres, bepaald via de route naar de switcher."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target or "8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="GoStream Tally bridge (preview/on-air op de telefoon)")
    parser.add_argument("--host", default=None,
                        help="IP-adres van de GoStream-switcher (leeg = wat je het laatst instelde, anders 192.168.1.80)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"TCP-poort van de switcher (standaard {SWITCHER_PORT})")
    parser.add_argument("--web-port", type=int, default=8080, help="poort van de webpagina (standaard 8080)")
    parser.add_argument("--labels", default="Cam 1,Cam 2,Cam 3,Cam 4,Cam 5,Cam 6,Cam 7,Cam 8",
                        help="komma-lijst met namen per input, in volgorde")
    parser.add_argument("--map", default=None,
                        help='bronnen omzetten, bijvoorbeeld "4001=5,4002=6" (leeg = niet omzetten)')
    parser.add_argument("--demo", action="store_true", help="simuleer een switcher (testen zonder hardware)")
    parser.add_argument("--no-open", action="store_true",
                        help="open de beheerpagina niet automatisch in de browser")
    args = parser.parse_args()

    labels = [part.strip() for part in args.labels.split(",") if part.strip()]
    state = TallyState(labels)
    if args.map is not None:
        state.aliases = parse_alias(args.map)
    opgeslagen = load_names()
    state.names.update(opgeslagen)
    state.labels = [state.name_for(i) for i in range(1, 9)]
    stop = threading.Event()

    # het adres van de switcher: wat je op de pc hebt ingesteld, of --host
    bewaard = load_switcher()
    host = args.host or bewaard.get("host") or "192.168.1.80"
    sport = args.port or bewaard.get("port") or SWITCHER_PORT
    target = Target(host, sport)

    state.configured_host = host
    eigen = local_ip("" if args.demo else host)
    if not eigen.startswith("127."):
        state.local_prefix = ".".join(eigen.split(".")[:3])

    if args.demo:
        threading.Thread(target=demo_loop, args=(state, labels, stop), daemon=True).start()
    else:
        state.set_connected(False, f"{host}: verbinden...")
        threading.Thread(target=switcher_loop, args=(state, target, stop), daemon=True).start()

    Handler.doel = target
    Handler.state = state
    EIGEN_ADRESSEN.update(eigen_adressen())
    toegang = Access()
    Handler.access = toegang

    ip = eigen
    print("")
    print("  GoStream Tally")
    print("  ---------------------------------------------")
    if args.demo:
        print("  Switcher: demo (simulated)")
    else:
        print(f"  Switcher: {host}:{sport}")

    # Windows kan poorten weigeren (WinError 10013): bezet, gereserveerd door
    # Hyper-V/WSL, of tegengehouden door firewall of antivirus. We proberen
    # daarom een reeks kandidaten voordat we opgeven.
    kandidaten = [args.web_port + i for i in range(10)]
    kandidaten += [18080 + i for i in range(10)]
    kandidaten += random.sample(range(20000, 60000), 40)

    server = None
    port = args.web_port
    laatste_fout = None
    for kandidaat in kandidaten:
        try:
            server = QuietServer(("0.0.0.0", kandidaat), Handler)
            port = kandidaat
            break
        except OSError as fout:
            laatste_fout = fout
            if kandidaat == args.web_port:
                print(f"  Port {kandidaat} is refused. Looking for a port that is allowed.")

    if server is None:
        print("")
        print("  Windows refuses every port I try.")
        print("  That is usually the firewall or a reserved port range.")
        print("  Check in PowerShell: netsh int ipv4 show excludedportrange protocol=tcp")
        print("  If 8080 is in that list, pick a port well outside it:")
        print("    python gostream_tally.py --web-port 45000")
        print("  If it keeps failing, add Python as an exception in the firewall.")
        print("")
        return

    print(f"  Open on your phone: http://{ip}:{port}")
    print("")
    print("  Manage access (this PC only):")
    print(f"    http://127.0.0.1:{port}/host")
    print("  Allow each device there. Without permission a phone sees")
    print("  no status at all and cannot control anything.")
    print("")
    print("  Stop: Ctrl+C")
    print("")
    sys.stdout.flush()

    # de beheerpagina meteen openen, zodat je alleen hoeft te klikken
    if not args.no_open:
        def _open_browser() -> None:
            time.sleep(1.0)
            try:
                webbrowser.open(f"http://127.0.0.1:{port}/host")
            except Exception:
                pass
        threading.Thread(target=_open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
