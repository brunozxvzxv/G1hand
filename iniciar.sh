#!/usr/bin/env bash
# Inicializador Linux: serve para o WSL (simulacao) e para o G1 (Linux nativo).
# Uso: ./iniciar.sh [opcoes do iniciar.py]   ex: ./iniciar.sh --forcar
cd "$(dirname "$0")" || exit 1

# Placa: normalmente nao precisa definir. O iniciar.py testa os dois bootloaders do Nano
# e lembra o que funcionou (iniciar_config.json). Para forcar: FQBN=arduino:avr:nano ./iniciar.sh
FQBN="${FQBN:-}"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
NO_WSL=1
grep -qi microsoft /proc/version 2>/dev/null && NO_WSL=0

# 1. Dependencias (so instala o que falta)
command -v python3 >/dev/null || { echo "[ERRO] Instale o python3."; exit 1; }

if ! python3 -c 'import serial' 2>/dev/null; then
  echo "Instalando pyserial..."
  $SUDO apt-get install -y python3-serial \
    || pip3 install --break-system-packages pyserial \
    || exit 1
fi

if [ ! -x ./arduino-cli ] && ! command -v arduino-cli >/dev/null; then
  echo "Instalando arduino-cli nesta pasta..."
  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
    | BINDIR="$PWD" sh || exit 1
fi

# 2. Porta serial
PORTAS=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null)
if [ -z "$PORTAS" ]; then
  echo "[ERRO] Nenhum Arduino em /dev/ttyUSB* ou /dev/ttyACM*. Confira o cabo USB."
  [ "$NO_WSL" -eq 0 ] && echo "No WSL: anexe com 'usbipd attach --wsl --busid X-Y' no PowerShell."
  exit 1
fi

if ! id -nG | grep -qw dialout; then
  echo "Adicionando $USER ao grupo dialout (vale a partir do proximo login)..."
  $SUDO usermod -aG dialout "$USER"
fi
for p in $PORTAS; do
  [ -w "$p" ] || $SUDO chmod 666 "$p"   # libera ja, sem precisar relogar
done

# 3. Sem tela (ex.: G1 por SSH): nao abre navegador e libera a visualizacao na rede
EXTRA=()
if [ "$NO_WSL" -eq 1 ] && [ -z "$DISPLAY$WAYLAND_DISPLAY" ]; then
  EXTRA+=(--sem-navegador --viz-host 0.0.0.0)
  echo "Sem tela. Veja a visualizacao em outro computador: http://$(hostname -I | awk '{print $1}'):8765/"
fi

[ -n "$FQBN" ] && EXTRA+=(--fqbn "$FQBN")

exec python3 iniciar.py "${EXTRA[@]}" "$@"
