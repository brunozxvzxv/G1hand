"""
Controle do servo no Arduino via USB (porta serial).

Instalação:  pip install pyserial

Uso:
  python servo_control.py                 # modo interativo (detecta a porta)
  python servo_control.py open            # abre e sai
  python servo_control.py close           # fecha e sai
  python servo_control.py angle 45        # move para 45 graus
  python servo_control.py -p COM3 open    # porta manual (Windows)
  python servo_control.py -p /dev/ttyACM0 open   # porta manual (Linux)
"""
import argparse
import sys
import time

import serial
import serial.tools.list_ports

BAUD = 9600


def find_arduino_port():
    """Procura uma porta que pareça ser um Arduino."""
    for p in serial.tools.list_ports.comports():
        desc = f"{p.description} {p.manufacturer or ''}".lower()
        if any(k in desc for k in ("arduino", "ch340", "usb serial", "acm", "wch", "ftdi")):
            return p.device
    ports = list(serial.tools.list_ports.comports())
    return ports[0].device if ports else None


class ServoController:
    def __init__(self, port=None, baud=BAUD):
        port = port or find_arduino_port()
        if not port:
            raise RuntimeError("Nenhuma porta serial encontrada. Conecte o Arduino via USB.")
        self.ser = serial.Serial(port, baud, timeout=2)
        time.sleep(2)  # o Arduino reinicia ao abrir a serial
        self.ser.reset_input_buffer()
        print(f"Conectado em {port}")

    def send(self, cmd):
        self.ser.write((cmd + "\n").encode())
        return self.ser.readline().decode(errors="ignore").strip()

    def open(self):
        return self.send("OPEN")

    def close(self):
        return self.send("CLOSE")

    def angle(self, n):
        return self.send(f"ANGLE {int(n)}")

    def status(self):
        return self.send("STATUS")

    def disconnect(self):
        self.ser.close()


def interactive(servo):
    print("Comandos: open | close | angle <0-180> | status | sair")
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
        elif line == "status":
            print(servo.status())
        elif line.startswith("angle "):
            try:
                print(servo.angle(line.split()[1]))
            except ValueError:
                print("Valor inválido")
        elif line:
            print("Comando desconhecido")


def main():
    ap = argparse.ArgumentParser(description="Controle de servo via Arduino/USB")
    ap.add_argument("-p", "--port", help="Porta serial (ex: COM3, /dev/ttyACM0)")
    ap.add_argument("action", nargs="?", choices=["open", "close", "angle", "status"])
    ap.add_argument("value", nargs="?", type=int, help="Ângulo (para 'angle')")
    args = ap.parse_args()

    try:
        servo = ServoController(args.port)
    except (RuntimeError, serial.SerialException) as e:
        sys.exit(f"Erro: {e}")

    try:
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
    finally:
        servo.disconnect()


if __name__ == "__main__":
    main()