#!/usr/bin/env python3
"""
Um comando só: atualiza o Arduino (se o .ino mudou), sobe o emissor.py com
visualização e abre o navegador. Sem IDE, sem VS Code.

Primeira vez:
  1. Instale o arduino-cli (ou deixe o arduino-cli.exe nesta pasta):
       Windows:  winget install ArduinoSA.CLI
       Linux:    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
  2. Dê dois cliques em iniciar.bat (Windows) ou rode ./iniciar.sh (Linux).

Nas próximas vezes o mesmo comando. O firmware só é recompilado/enviado se o
servo_usb.ino (ou a placa) mudou.

Uso:
  python iniciar.py                    # tudo automático
  python iniciar.py -p COM3            # porta manual
  python iniciar.py --fqbn arduino:avr:nano:cpu=atmega328old   # Nano com bootloader antigo
  python iniciar.py --reset-manual     # guia o RESET manual do Nano (WSL)
  python iniciar.py --forcar           # reenvia o firmware mesmo sem mudanças
  python iniciar.py --so-firmware      # só atualiza o Arduino e sai
  python iniciar.py --interativo       # terminal de comandos em vez de ficar parado
  python iniciar.py --sem-navegador

No seu script do G1, para garantir o firmware antes de controlar a garra:
  from iniciar import garantir_firmware
  porta = garantir_firmware()
  servo = ServoController(porta)
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser

AQUI = os.path.dirname(os.path.abspath(__file__))
if AQUI not in sys.path:
    sys.path.insert(0, AQUI)
SKETCH = os.path.join(AQUI, "servo_usb.ino")
BUILD = os.path.join(AQUI, ".build")
CONFIG = os.path.join(AQUI, "iniciar_config.json")
FQBN_PADRAO = "arduino:avr:nano"


# ----------------------------------------------------------------- utilidades
def garantir_pyserial():
    try:
        import serial  # noqa: F401
        return
    except ImportError:
        pass
    print("Instalando pyserial...")
    base = [sys.executable, "-m", "pip", "install", "pyserial"]
    if subprocess.call(base) != 0:
        subprocess.check_call(base + ["--break-system-packages"])


def carregar_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def salvar_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def achar_cli():
    exe = "arduino-cli.exe" if os.name == "nt" else "arduino-cli"
    local = os.path.join(AQUI, exe)
    return local if os.path.exists(local) else shutil.which("arduino-cli")


def rodar(cmd, descricao, fatal=True, timeout=None):
    """Roda um comando; só mostra a saída se falhar. Com fatal=False devolve None em vez de sair."""
    print(f"  {descricao}...")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"  Tempo esgotado ({timeout}s).")
        r = None
    if r is None or r.returncode != 0:
        if r is not None:
            linhas = (r.stdout + "\n" + r.stderr).strip().splitlines()
            print("\n".join(linhas[-8:]))   # só o final; o avrdude repete a mesma linha 10 vezes
        if fatal:
            sys.exit(f"Falhou: {descricao}")
        return None
    return r.stdout


def detectar_placa(cli):
    """Devolve (porta, fqbn) da primeira placa reconhecida pelo arduino-cli."""
    try:
        r = subprocess.run([cli, "board", "list", "--format", "json"],
                           capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, None
    itens = data.get("detected_ports", []) if isinstance(data, dict) else data  # v1.x / v0.x
    for item in itens:
        porta = item.get("port", item)
        boards = item.get("matching_boards") or item.get("boards") or []
        if boards and porta.get("address"):
            return porta["address"], boards[0].get("fqbn")
    return None, None


def achar_porta():
    """Procura uma porta que pareça ser um Arduino (não depende do emissor.py)."""
    garantir_pyserial()
    import serial.tools.list_ports
    portas = list(serial.tools.list_ports.comports())
    for p in portas:
        desc = f"{p.description} {p.manufacturer or ''}".lower()
        if any(k in desc for k in ("arduino", "ch340", "usb serial", "acm", "wch", "ftdi", "usb-serial")):
            return p.device
    # fallback: primeira porta USB, ignorando as portas de sistema (/dev/ttyS*, COM1 do WSL)
    for p in portas:
        if "ttyACM" in p.device or "ttyUSB" in p.device or p.device.upper().startswith("COM"):
            return p.device
    return None


def quem_usa_porta(porta):
    """Linux: lista (pid, nome) de outros processos que estão com a porta serial aberta."""
    achados, meu = [], os.getpid()
    try:
        real = os.path.realpath(porta)
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == meu:
                continue
            try:
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    try:
                        if os.path.realpath(f"/proc/{pid}/fd/{fd}") == real:
                            with open(f"/proc/{pid}/comm") as f:
                                achados.append((int(pid), f.read().strip()))
                            break
                    except OSError:
                        pass
            except OSError:
                pass
    except OSError:
        pass
    return achados


def aguardar_reset_manual():
    """Guia o reset manual do Nano (útil no WSL, onde o reset automático pelo USB falha)."""
    print("  RESET manual: SEGURE o botão RESET do Nano e solte quando aparecer AGORA.")
    for n in (3, 2, 1):
        print(f"    {n}...", flush=True)
        time.sleep(1)
    print("    AGORA! (solte o RESET)", flush=True)


FIRMWARE_OK = True   # False se o envio falhou e a placa segue com o firmware antigo


def checar_arquivos():
    faltando = [n for n in ("emissor.py", "servo_usb.ino", "demo_garra_g1.html")
                if not os.path.exists(os.path.join(AQUI, n))]
    if faltando:
        sys.exit("Arquivos que faltam na pasta " + AQUI + ":\n  - " + "\n  - ".join(faltando) +
                 "\nCopie-os para a mesma pasta do iniciar.py (o emissor.py deve ter exatamente esse nome).")


# ------------------------------------------------------------------- firmware
def garantir_firmware(porta=None, fqbn=None, forcar=False, reset_manual=False):
    """Compila e envia o servo_usb.ino SOMENTE se ele (ou a placa) mudou.
    Devolve a porta serial do Arduino. Se o envio falhar, NÃO encerra: avisa e segue
    com o firmware que já está na placa (FIRMWARE_OK fica False)."""
    global FIRMWARE_OK
    FIRMWARE_OK = True
    cfg = carregar_config()
    cli = achar_cli()

    det_porta, det_fqbn = detectar_placa(cli) if cli else (None, None)
    explicito = bool(fqbn)
    fqbn = fqbn or cfg.get("fqbn") or det_fqbn or FQBN_PADRAO
    porta = porta or det_porta or achar_porta()
    # /dev/ttyUSB* (FTDI/CH340) é Nano, nunca Uno (o Uno aparece em ttyACM). O
    # arduino-cli às vezes devolve "uno" para esses chips e o upload trava.
    if not explicito and porta and "ttyUSB" in porta and fqbn == "arduino:avr:uno":
        fqbn = FQBN_PADRAO
    if not porta:
        sys.exit("Nenhum Arduino encontrado (esperado /dev/ttyACM0 ou /dev/ttyUSB0).\n"
                 "No WSL, anexe o Arduino com: usbipd attach --wsl --busid SEU_BUSID (PowerShell do Windows).")

    with open(SKETCH, "rb") as f:
        assinatura = hashlib.sha1(f.read() + fqbn.encode()).hexdigest()

    if not forcar and cfg.get("hash") == assinatura:
        print(f"Firmware já atualizado ({fqbn}) em {porta}.")
        return porta

    if not cli:
        sys.exit(
            "O firmware precisa ser enviado, mas o arduino-cli não foi encontrado.\n"
            "  Windows: winget install ArduinoSA.CLI   (ou ponha arduino-cli.exe nesta pasta)\n"
            "  Linux:   curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh"
        )

    print(f"Atualizando firmware em {porta} ({fqbn})")
    plataforma = ":".join(fqbn.split(":")[:2])
    if plataforma not in rodar([cli, "core", "list"], "verificando núcleo da placa"):
        rodar([cli, "core", "update-index"], "baixando índice de placas")
        rodar([cli, "core", "install", plataforma], f"instalando núcleo {plataforma}")

    if "Servo" not in rodar([cli, "lib", "list"], "verificando biblioteca Servo"):
        rodar([cli, "lib", "install", "Servo"], "instalando biblioteca Servo")

    # o arduino-cli exige a pasta com o mesmo nome do .ino
    pasta = os.path.join(BUILD, "servo_usb")
    os.makedirs(pasta, exist_ok=True)
    shutil.copy(SKETCH, os.path.join(pasta, "servo_usb.ino"))

    # Nano: o bootloader pode ser o novo ou o antigo (clones). Tenta os dois.
    # Se a placa não foi pedida na linha de comando, tenta também as outras opções AVR.
    candidatos = [fqbn]
    if fqbn.startswith("arduino:avr:nano"):
        alt = "arduino:avr:nano" if "atmega328old" in fqbn else "arduino:avr:nano:cpu=atmega328old"
        candidatos.append(alt)
    if not explicito and fqbn.startswith("arduino:avr:"):
        extras = ["arduino:avr:nano", "arduino:avr:nano:cpu=atmega328old"]
        if "ttyUSB" not in porta:          # Uno só aparece em ttyACM / COM
            extras.append("arduino:avr:uno")
        for extra in extras:
            if extra not in candidatos:
                candidatos.append(extra)

    # a porta precisa estar livre: outro iniciar.py/emissor.py aberto faz o upload falhar
    em_uso = quem_usa_porta(porta)
    if em_uso:
        FIRMWARE_OK = False
        print(f"\nA porta {porta} está em uso por: " + ", ".join(f"{n} (PID {p})" for p, n in em_uso))
        print("Encerre (ex.: kill " + " ".join(str(p) for p, _ in em_uso) + ") e rode de novo.")
        print("Seguindo com o firmware que já está na placa.\n")
        return porta

    enviado = None
    for f in candidatos:
        rodar([cli, "compile", "--fqbn", f, pasta], "compilando")
        if reset_manual:
            aguardar_reset_manual()
        if rodar([cli, "upload", "-p", porta, "--fqbn", f, pasta],
                 f"enviando para o Arduino ({f})", fatal=False, timeout=90) is not None:
            enviado = f
            break
        print("  Falhou.")

    if enviado is None:
        FIRMWARE_OK = False
        print("\n[AVISO] Não foi possível enviar o firmware. A placa segue com o firmware ANTIGO\n"
              "        (mudanças no servo_usb.ino, como o ângulo, só valem depois do envio).\n"
              "  Tente: ./iniciar.sh --reset-manual   (aperte o RESET do Nano na contagem)\n"
              "  Ou grave pela Arduino IDE no Windows e anexe a placa ao WSL de novo.\n"
              "  O envio será tentado de novo na próxima execução.\n")
        return porta

    # assinatura sempre do fqbn que funcionou
    with open(SKETCH, "rb") as f:
        assinatura = hashlib.sha1(f.read() + enviado.encode()).hexdigest()
    cfg.update(hash=assinatura, fqbn=enviado)
    salvar_config(cfg)
    print("Firmware atualizado.")
    time.sleep(1)  # a placa reinicia após o upload
    return porta


def abrir_navegador(url):
    """Abre o navegador; no WSL usa o navegador do Windows."""
    try:
        with open("/proc/version") as f:
            wsl = "microsoft" in f.read().lower()
    except OSError:
        wsl = False
    if wsl:
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)


# ----------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Atualiza o firmware e inicia o emissor com visualização")
    ap.add_argument("-p", "--port", help="Porta serial (ex: COM3, /dev/ttyACM0)")
    ap.add_argument("--fqbn", help="Placa (padrão: detecta, senão arduino:avr:nano)")
    ap.add_argument("--forcar", action="store_true", help="Reenvia o firmware mesmo sem mudanças")
    ap.add_argument("--reset-manual", action="store_true",
                    help="Guia o reset manual do Nano antes de cada envio (WSL com reset automático falhando)")
    ap.add_argument("--so-firmware", action="store_true", help="Só atualiza o Arduino e sai")
    ap.add_argument("--interativo", action="store_true", help="Abre o terminal de comandos")
    ap.add_argument("--sem-navegador", action="store_true")
    ap.add_argument("--viz-port", type=int, default=8765)
    ap.add_argument("--viz-host", default="127.0.0.1")
    args = ap.parse_args()

    checar_arquivos()
    garantir_pyserial()
    porta = garantir_firmware(args.port, args.fqbn, args.forcar, args.reset_manual)
    if args.so_firmware:
        sys.exit(0 if FIRMWARE_OK else 1)

    import emissor
    try:
        servo = emissor.ServoController(porta)
        servo.start_viz(args.viz_port, args.viz_host)
    except OSError as e:
        sys.exit(f"Erro: {e}\nA porta {args.viz_port} ou a serial já está em uso (outro emissor aberto? "
                 f"monitor serial da IDE?).")
    except Exception as e:
        sys.exit(f"Erro: {e}")

    url = f"http://localhost:{args.viz_port}/"
    if not args.sem_navegador:
        abrir_navegador(url)
    try:
        if args.interativo:
            emissor.interactive(servo)
        else:
            print(f"Rodando em {url}  (Ctrl+C para sair)")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        servo.disconnect()


if __name__ == "__main__":
    main()
