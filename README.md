# Garra G1

Controle de uma garra de pinça paralela acionada por um servo em um **Arduino Nano**, comandada por
**serial USB (UART, 9600 baud)** a partir de um computador Linux (ou WSL). Inclui um painel web de
visualização/comando e um módulo Python para usar a garra em outros scripts (ex.: controle do Unitree G1).

## Como a garra se comporta

| Situação | Ângulo do servo | Garra |
|---|---|---|
| Repouso / ao ligar / ao soltar o botão | **100°** | Aberta |
| Segurando o botão (barra de espaço no painel) | desce até **20°** | Fechando / fechada |

- O curso é limitado por software a **20°–100°** (`ANGLE_CLOSE` e `ANGLE_OPEN`). Nenhum comando passa disso.
- **Homem-morto:** a garra só continua fechando enquanto o comando `GRIP` for reenviado (a cada 100 ms).
  Se o sinal parar por mais de 400 ms (processo travou, cabo solto, navegador fechado), o Arduino volta
  sozinho para a posição aberta.

## Estrutura

| Arquivo | Função |
|---|---|
| `servo_usb.ino` | Firmware do Arduino: recebe comandos pela serial e move o servo (pino `A1`). |
| `emissor.py` | Dono da porta serial. Classe `ServoController`, CLI e servidor HTTP do painel. |
| `demo_garra_g1.html` | Painel web: desenho da garra, telemetria, gráfico e registro. Funciona também em modo simulação. |
| `iniciar.py` | Um comando só: grava o firmware (se mudou), sobe o `emissor.py` e abre o navegador. |
| `iniciar.sh` | Prepara o ambiente Linux/WSL (pyserial, arduino-cli, permissão da serial) e chama o `iniciar.py`. |
| `requirements.txt` | Dependência Python (`pyserial`). |

Os cinco arquivos de código precisam ficar **na mesma pasta**: o `iniciar.py` e o `emissor.py` procuram
`servo_usb.ino` e `demo_garra_g1.html` ao lado deles.

## Hardware

- Arduino Nano (testado com os dois bootloaders; o `iniciar.py` tenta os dois).
- Servo com o sinal no pino **A1**. Use alimentação externa para o servo, com **GND comum** com o Arduino.
- *Opcional:* sensor de corrente (INA219 / ACS712 ou shunt) no pino **A0** para detectar contato com um
  objeto. Vem **desligado** (`USE_CURRENT_SENSOR = false` no firmware); sem o sensor, o A0 solto gera
  contato falso. Os detalhes de ligação e calibração estão no fim do `servo_usb.ino`.

## Uso rápido

```bash
bash iniciar.sh
```
ou

```bash
chmod +x iniciar.sh      # só na primeira vez, se o git não preservou a permissão
./iniciar.sh
```

O `iniciar.sh` instala o que faltar (`pyserial`, `arduino-cli`), libera a porta serial, e o `iniciar.py`
detecta a placa, compila e grava o firmware **somente se o `.ino` ou a placa mudou**, sobe o servidor e abre
`http://localhost:8765/`. Sem tela (G1 por SSH) ele não abre o navegador e libera o painel na rede, na porta 8765.

Opções úteis do `iniciar.py` (o `iniciar.sh` repassa todas):

```bash
./iniciar.sh -p /dev/ttyACM0       # porta manual
./iniciar.sh --forcar              # regrava o firmware mesmo sem mudanças
./iniciar.sh --reset-manual        # guia o RESET manual do Nano (útil no WSL)
./iniciar.sh --so-firmware         # só atualiza o Arduino e sai
./iniciar.sh --interativo          # terminal de comandos em vez de ficar parado
./iniciar.sh --sem-navegador
FQBN=arduino:avr:nano:cpu=atmega328old ./iniciar.sh   # força a placa/bootloader
```

### WSL

Anexe o Arduino ao WSL antes (PowerShell do Windows): `usbipd attach --wsl --busid SEU_BUSID`.
Se o reset automático falhar no upload, use `--reset-manual`.

### Sem Arduino (simulação)

Abra `demo_garra_g1.html` direto no navegador: sem o `emissor.py` ele roda em simulação (segure a barra de
espaço para fechar). Aberto como arquivo ele **só mostra o estado**; para comandar a garra real, acesse
`http://localhost:8765/`.

## Usar em outro script (ex.: Unitree G1)

```python
from iniciar import garantir_firmware
from emissor import ServoController

porta = garantir_firmware()               # grava o firmware só se mudou
servo = ServoController(porta)            # ou ServoController() para detectar a porta
servo.start_viz()                         # opcional: painel em http://localhost:8765

resultado, angulo = servo.grab(timeout=5) # fecha e segura
if resultado == "contact":                # sentiu o objeto (só com sensor de corrente)
    ...                                   # mova o braço aqui
servo.release()                           # solta e volta a abrir

with servo.holding():                     # alternativa: garante que solta ao sair do bloco
    ...
```

`grab()` devolve `("contact" | "limit" | "released" | "timeout", ângulo)`. `limit` significa que a garra
fechou até o fim sem encontrar nada. A garra **continua segurando** (enquanto o processo estiver vivo) até
você chamar `release()`.

Outros métodos: `open()`, `close()`, `angle(n)` (limitado a 20–100), `status()`, `current()`, `disconnect()`.

CLI do `emissor.py`:

```bash
python emissor.py                    # modo interativo
python emissor.py open | close | status
python emissor.py angle 45
python emissor.py grip               # fecha, segura até Enter, depois abre
python emissor.py -p /dev/ttyACM0 --viz
```

## Protocolo serial (9600 baud, comandos terminados em `\n`)

| Comando | Efeito |
|---|---|
| `GRIP` | Fecha enquanto for reenviado a cada < 400 ms (homem-morto). |
| `RELEASE` | Sai do modo `GRIP` e volta aos poucos para a posição aberta. |
| `OPEN` / `CLOSE` | Vai direto para 100° / 20°. |
| `ANGLE <n>` | Move para `n` graus, limitado a 20–100. |
| `STATUS` | `STATUS ANGLE <n> GRIP <0\|1> CONTACT <0\|1>` |
| `CURRENT` | Leitura do sensor de corrente (para calibrar o limiar). |

Mensagens espontâneas do Arduino: `READY`, `ANGLE <n>` (telemetria), `CONTACT <n>`, `LIMIT <n>`, `RELEASED`.

## API HTTP do `emissor.py` (porta 8765)

| Rota | Uso |
|---|---|
| `GET /` | Serve o `demo_garra_g1.html`. |
| `GET /state?since=<id>` | Estado em JSON (ângulo, conexão, contato, latência) e novas linhas do registro. |
| `POST /grip` | Heartbeat de fechamento (o painel envia a cada 100 ms enquanto o botão está apertado). |
| `POST /release` | Solta. |
| `POST /open`, `POST /angle?v=<n>` | Comandos diretos (o painel não usa; úteis para teste). |

Os `POST` aceitam `?n=<número crescente>` para descartar comandos que chegam fora de ordem.

> **Segurança:** por padrão o servidor escuta só em `127.0.0.1`. Com `--viz-host 0.0.0.0` (o `iniciar.sh` faz
> isso sozinho quando não há tela) **qualquer máquina da rede consegue comandar a garra**. Use só em rede
> confiável.

## Ajustar o curso da garra

Os limites **20° / 100°** estão escritos em três lugares e precisam ser mantidos iguais:

1. `servo_usb.ino` → `ANGLE_OPEN` e `ANGLE_CLOSE`
2. `emissor.py` → `ANGLE_OPEN` e `ANGLE_CLOSE`
3. `demo_garra_g1.html` → `OPEN` e `CLOSE`

Depois de alterar o `.ino`, rode `./iniciar.sh` (ele regrava sozinho). Ajuste de 1° em 1° e teste sem objeto
para não forçar o servo no fim do curso. Se a garra se mover ao contrário do esperado, troque os valores de
abertura e fechamento nos três arquivos.

## Solução de problemas

- **"Nenhum Arduino encontrado":** confira o cabo; no WSL, anexe com `usbipd`.
- **"A porta está em uso":** outro `iniciar.py`/`emissor.py` ou o monitor serial da IDE está aberto. Encerre e rode de novo.
- **Upload falha no Nano:** o `iniciar.py` já tenta os dois bootloaders; se ainda falhar, `--reset-manual`.
- **Permissão negada na serial:** o `iniciar.sh` adiciona o usuário ao grupo `dialout` (vale no próximo login) e libera a porta na hora.
- **Garra para antes do fim com o sensor desligado:** confirme `USE_CURRENT_SENSOR = false` se não há sensor ligado em `A0`.
