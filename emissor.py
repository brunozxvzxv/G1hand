"""
Controle da garra (servo no Arduino) via USB. Este script é o ÚNICO dono da porta
serial. O HTML só visualiza: ele lê o estado deste script por HTTP.

Instalação:  pip install pyserial

------------------------------------------------------------------------------
USO COMO MÓDULO (no seu script de controle do Unitree G1)
------------------------------------------------------------------------------
    from emissor import ServoController

    servo = ServoController("/dev/ttyACM0")   # ou None para detectar a porta
    servo.start_viz()                         # opcional: http://localhost:8765

    resultado, angulo = servo.grab(timeout=5) # fecha e segura
    if resultado == "contact":                # sentiu o objeto, parou de forçar
        ...                                   # mova o braço do G1 aqui
    servo.release()                           # abre

    # ou, garantindo que abre ao sair do bloco (mesmo com erro):
    with servo.holding():
        ...

IMPORTANTE (homem-morto): o Arduino só continua fechado enquanto recebe "GRIP".
Este módulo reenvia GRIP a cada 100 ms numa thread. Se o seu processo travar,
cair ou fechar a serial, a garra ABRE sozinha em ~400 ms (e solta o objeto).

------------------------------------------------------------------------------
USO NA LINHA DE COMANDO
------------------------------------------------------------------------------
  python emissor.py                       # modo interativo (detecta a porta)
  python emissor.py open
  python emissor.py close
  python emissor.py angle 45
  python emissor.py grip                  # fecha, segura até Enter, depois abre
  python emissor.py status
  python emissor.py -p COM3 grip          # porta manual (Windows)
  python emissor.py -p /dev/ttyACM0 --viz # com servidor de visualização
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import serial
import serial.tools.list_ports

BAUD = 9600
HB_TIMEOUT = 0.35         # sem sinal do navegador por esse tempo -> solta a garra
GRIP_PERIOD = 0.1          # reenvio do GRIP (s); o Arduino abre se passar de 400 ms
# Polaridade: a garra repousa ABERTA (100). Segurar espaço (GRIP) FECHA a garra (100 -> 20); soltar volta a abrir.
# Estes valores precisam ser iguais aos do servo_usb.ino.
ANGLE_OPEN = 100           # abertura máxima
ANGLE_CLOSE = 20           # fechamento
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_garra_g1.html")


def find_arduino_port():
    """Procura uma porta que pareça ser um Arduino."""
    for p in serial.tools.list_ports.comports():
        desc = f"{p.description} {p.manufacturer or ''}".lower()
        if any(k in desc for k in ("arduino", "ch340", "usb serial", "acm", "wch", "ftdi")):
            return p.device
    ports = list(serial.tools.list_ports.comports())
    return ports[0].device if ports else None


def _to_int(text, default=None):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


class ServoController:
    def __init__(self, port=None, baud=BAUD):
        port = port or find_arduino_port()
        if not port:
            raise RuntimeError("Nenhuma porta serial encontrada. Conecte o Arduino via USB.")
        self.port = port
        self.ser = serial.Serial(port, baud, timeout=0.1)
        time.sleep(2)  # o Arduino reinicia ao abrir a serial
        self.ser.reset_input_buffer()

        self._state_lock = threading.Lock()
        self._cmd_lock = threading.Lock()   # pareia comando <-> resposta
        self._wr_lock = threading.Lock()    # protege ser.write (curto)
        self._resp = queue.Queue()
        self._log = deque(maxlen=200)
        self._log_id = 0
        self._state = {
            "connected": True, "port": port, "angle": ANGLE_OPEN,
            "gripping": False, "contact": False, "limit": False,
            "last": "", "latency_ms": None,
        }

        self._result = None
        self._result_evt = threading.Event()
        self._grip_stop = threading.Event()
        self._grip_thread = None
        self._server = None
        self._hb_required = False
        self._hb = 0.0
        self._last_seq = 0
        self._seq_lock = threading.Lock()

        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._add_log(f"Conectado em {port}")
        print(f"Conectado em {port}")

    # ------------------------------------------------------------------ estado
    def _set(self, **kw):
        with self._state_lock:
            self._state.update(kw)

    def _add_log(self, text):
        with self._state_lock:
            self._log_id += 1
            self._log.append((self._log_id, text))

    def snapshot(self, since=0):
        with self._state_lock:
            s = dict(self._state)
            s["log"] = [[i, t] for i, t in self._log if i > since]
            s["log_id"] = self._log_id
        return s

    @property
    def state(self):
        with self._state_lock:
            return dict(self._state)

    # ------------------------------------------------------------ leitura serial
    def _read_loop(self):
        buf = b""
        while self._running:
            try:
                data = self.ser.read(64)
            except (serial.SerialException, OSError) as e:
                self._set(connected=False)
                self._add_log(f"Erro serial: {e}")
                break
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode(errors="ignore").strip()
                if line:
                    self._handle_line(line)

    def _finish_grip(self, kind, angle):
        self._result = (kind, angle)
        self._result_evt.set()

    def _handle_line(self, line):
        self._add_log("RX " + line)
        parts = line.split()
        head = parts[0]

        # --- eventos assíncronos (não entram na fila de respostas)
        if head == "ANGLE" and len(parts) > 1:
            self._set(angle=_to_int(parts[1], self.state["angle"]))
        elif head == "CONTACT":
            a = _to_int(parts[1] if len(parts) > 1 else None, self.state["angle"])
            self._set(angle=a, contact=True)
            self._finish_grip("contact", a)
        elif head == "LIMIT":
            a = _to_int(parts[1] if len(parts) > 1 else None, ANGLE_CLOSE)
            self._set(angle=a, limit=True)
            self._finish_grip("limit", a)
        elif head == "RELEASED":   # o Arduino abriu sozinho (GRIP parou de chegar)
            self._set(gripping=False, contact=False, limit=False)
            self._finish_grip("released", self.state["angle"])
        elif line == "OK GRIP" or head == "READY":
            pass
        else:
            # --- respostas a comandos
            self._set(last=line)
            if line == "OK OPEN":
                self._set(angle=ANGLE_OPEN, gripping=False, contact=False, limit=False)
            elif line == "OK RELEASE":   # volta ao repouso aos poucos; o ângulo chega pela telemetria
                self._set(gripping=False, contact=False, limit=False)
            elif line == "OK CLOSE":
                self._set(angle=ANGLE_CLOSE)
            elif line.startswith("OK ANGLE"):
                self._set(angle=_to_int(parts[-1], self.state["angle"]))
            elif head == "STATUS" and "ANGLE" in parts:
                self._set(angle=_to_int(parts[parts.index("ANGLE") + 1], self.state["angle"]))
            self._resp.put(line)

    # ------------------------------------------------------------------- envio
    def _write(self, cmd, log=True):
        if log:
            self._add_log("TX " + cmd)
        with self._wr_lock:
            self.ser.write((cmd + "\n").encode())

    def send(self, cmd, timeout=2.0):
        """Envia um comando e devolve a linha de resposta ('' se não houver)."""
        with self._cmd_lock:
            while not self._resp.empty():      # descarta respostas antigas
                self._resp.get_nowait()
            t0 = time.perf_counter()
            self._write(cmd)
            try:
                line = self._resp.get(timeout=timeout)
            except queue.Empty:
                return ""
            self._set(latency_ms=round((time.perf_counter() - t0) * 1000))
            return line

    # ------------------------------------------------------------ comandos básicos
    def open(self):
        self._stop_grip_thread()
        return self.send("OPEN")

    def close(self):
        self._stop_grip_thread()
        return self.send("CLOSE")

    def angle(self, n):
        self._stop_grip_thread()
        n = max(ANGLE_CLOSE, min(ANGLE_OPEN, int(n)))   # respeita o curso da garra
        return self.send(f"ANGLE {n}")

    def status(self):
        return self.send("STATUS")

    def current(self):
        return self.send("CURRENT")

    # ------------------------------------------------------------ segurar / soltar
    def _grip_loop(self):
        while not self._grip_stop.wait(GRIP_PERIOD):
            # aperto comandado pelo navegador: precisa de "heartbeat" contínuo
            if self._hb_required and time.monotonic() - self._hb > HB_TIMEOUT:
                self._add_log("Navegador parou de enviar sinal: soltando")
                self.release()
                break
            try:
                self._write("GRIP", log=False)
            except (serial.SerialException, OSError):
                self._set(connected=False)
                break

    def grip_start(self, heartbeat=False):
        """Começa a fechar e mantém o GRIP ativo (reenvio automático).
        heartbeat=True: só segura enquanto grip_heartbeat() continuar sendo chamado."""
        if self._grip_thread and self._grip_thread.is_alive():
            return
        self._hb_required = heartbeat
        self._hb = time.monotonic()
        self._result = None
        self._result_evt.clear()
        self._set(gripping=True, contact=False, limit=False)
        self._write("GRIP")
        self._grip_stop.clear()
        self._grip_thread = threading.Thread(target=self._grip_loop, daemon=True)
        self._grip_thread.start()

    def grip_heartbeat(self):
        """Chamado pelo navegador (~100 ms) enquanto o botão/espaço está pressionado."""
        self._hb = time.monotonic()
        if not (self._grip_thread and self._grip_thread.is_alive()):
            self.grip_start(heartbeat=True)

    def accept_seq(self, seq):
        """Descarta comandos atrasados do navegador (chegaram fora de ordem)."""
        with self._seq_lock:
            if seq < self._last_seq:
                return False
            self._last_seq = seq
            return True

    def wait_grip_result(self, timeout=5.0):
        """Espera ('contact'|'limit'|'released', ângulo) ou ('timeout', ângulo)."""
        if self._result_evt.wait(timeout):
            return self._result
        return ("timeout", self.state["angle"])

    def grab(self, timeout=5.0):
        """Fecha e espera: 'contact' (pegou algo), 'limit' (fechou vazia) ou 'timeout'.
        A garra continua segurando até você chamar release()."""
        self.grip_start()
        return self.wait_grip_result(timeout)

    def _stop_grip_thread(self):
        self._grip_stop.set()
        t = self._grip_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1)
        self._grip_thread = None
        self._set(gripping=False, contact=False, limit=False)

    def release(self):
        """Para de enviar GRIP e abre."""
        self._stop_grip_thread()
        return self.send("RELEASE")

    @contextmanager
    def holding(self):
        self.grip_start()
        try:
            yield self
        finally:
            self.release()

    # ------------------------------------------------------------- visualização
    def start_viz(self, port=8765, host="127.0.0.1"):
        """Sobe um servidor HTTP só-leitura para o demo_garra_g1.html."""
        if host not in ("127.0.0.1", "localhost"):
            print("ATENÇÃO: o servidor está acessível pela rede e aceita comandos da garra.")
        handler = type("VizHandler", (_VizHandler,), {"servo": self})
        self._server = ThreadingHTTPServer((host, port), handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        print(f"Visualização: http://{'localhost' if host == '127.0.0.1' else host}:{port}/")
        return self._server

    # ----------------------------------------------------------------- encerrar
    def disconnect(self):
        try:
            self._stop_grip_thread()
        finally:
            self._running = False
            if self._server:
                self._server.shutdown()
            self._reader.join(timeout=1)
            self.ser.close()


class _VizHandler(BaseHTTPRequestHandler):
    servo = None

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/state":
            since = 0
            for kv in query.split("&"):
                if kv.startswith("since="):
                    since = _to_int(kv[6:], 0)
            body = json.dumps(self.servo.snapshot(since)).encode()
            self._send(200, "application/json", body)
        elif path in ("/", "/index.html") and os.path.exists(HTML_FILE):
            with open(HTML_FILE, "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        else:
            self._send(404, "text/plain", b"nao encontrado")

    def _origin_ok(self):
        """Comandos só da própria página servida por este script (anti-CSRF / DNS rebinding)."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        bind, port = self.server.server_address[:2]
        if bind in ("127.0.0.1", "localhost") and host not in (
                f"localhost:{port}", f"127.0.0.1:{port}"):
            return False
        return origin is None or origin == f"http://{host}"

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if not self._origin_ok():
            return self._send(403, "application/json", b'{"erro":"origem nao permitida"}')
        q = parse_qs(query)
        seq = _to_int(q.get("n", [None])[0])
        if seq is not None and not self.servo.accept_seq(seq):
            return self._send(200, "application/json", b'{"ignorado":true}')
        try:
            if path == "/grip":
                self.servo.grip_heartbeat()
            elif path == "/release":
                self.servo.release()
            elif path == "/open":
                self.servo.open()
            elif path == "/angle":
                v = _to_int(q.get("v", [None])[0])
                if v is None:
                    return self._send(400, "application/json", b'{"erro":"v invalido"}')
                self.servo.angle(v)
            else:
                return self._send(404, "application/json", b'{"erro":"nao encontrado"}')
        except (serial.SerialException, OSError) as e:
            return self._send(500, "application/json", json.dumps({"erro": str(e)}).encode())
        self._send(200, "application/json", b'{"ok":true}')

    def log_message(self, *args):
        pass


# ======================================================================= CLI
def interactive(servo):
    print("Comandos: open | close | angle <20-100> | grip | release | status | current | sair")
    while True:
        try:
            line = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if line in ("sair", "exit", "q"):
            break
        elif line == "open":
            print(servo.open())
        elif line == "close":
            print(servo.close())
        elif line == "grip":
            print("resultado:", servo.grab(timeout=5), "(segurando; use 'release' para soltar)")
        elif line == "release":
            print(servo.release())
        elif line == "status":
            print(servo.status())
        elif line == "current":
            print(servo.current())
        elif line.startswith("angle "):
            try:
                print(servo.angle(line.split()[1]))
            except ValueError:
                print("Valor inválido")
        elif line:
            print("Comando desconhecido")


def main():
    ap = argparse.ArgumentParser(description="Controle da garra via Arduino/USB")
    ap.add_argument("-p", "--port", help="Porta serial (ex: COM3, /dev/ttyACM0)")
    ap.add_argument("action", nargs="?", choices=["open", "close", "angle", "grip", "status"])
    ap.add_argument("value", nargs="?", type=int, help="Ângulo (para 'angle')")
    ap.add_argument("--timeout", type=float, default=5.0, help="Tempo máx. esperando contato no 'grip'")
    ap.add_argument("--viz", action="store_true", help="Sobe o servidor para o demo_garra_g1.html")
    ap.add_argument("--viz-port", type=int, default=8765)
    ap.add_argument("--viz-host", default="127.0.0.1", help="Use 0.0.0.0 para ver de outro computador")
    args = ap.parse_args()

    try:
        servo = ServoController(args.port)
    except (RuntimeError, serial.SerialException) as e:
        sys.exit(f"Erro: {e}")

    try:
        if args.viz:
            servo.start_viz(args.viz_port, args.viz_host)

        if args.action is None:
            interactive(servo)
        elif args.action == "open":
            print(servo.open())
        elif args.action == "close":
            print(servo.close())
        elif args.action == "status":
            print(servo.status())
        elif args.action == "angle":
            if args.value is None:
                sys.exit("Informe o ângulo. Ex: angle 45")
            print(servo.angle(args.value))
        elif args.action == "grip":
            print("resultado:", servo.grab(args.timeout))
            try:
                input("Segurando. Enter (ou Ctrl+C) para soltar... ")
            except (EOFError, KeyboardInterrupt):
                pass
            print(servo.release())

        if args.viz and args.action not in (None, "grip"):
            print("Servidor de visualização ativo. Ctrl+C para sair.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        servo.disconnect()


if __name__ == "__main__":
    main()
