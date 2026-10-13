/*
  Garra G1 - Servo via USB (Serial)
  Servo - sinal    -> A1
  Sensor de corrente (saída analógica) -> A0   (veja o README no fim do arquivo)

  Comandos (enviar com \n no final):
    GRIP        -> FECHA a garra ENQUANTO for reenviado a cada <400 ms (homem-morto).
                   Se parar de chegar, a garra volta sozinha para a posição de repouso (aberta).
    RELEASE     -> volta para a posição de repouso (aberta), também sai do modo GRIP
    OPEN        -> vai direto para a abertura máxima
    CLOSE       -> vai direto para a posição fechada
    ANGLE <n>   -> move para n graus (limitado a ANGLE_CLOSE..ANGLE_OPEN = 20-100)
    STATUS      -> retorna "STATUS ANGLE <n> GRIP <0|1> CONTACT <0|1>"
    CURRENT     -> retorna leitura do sensor (para calibrar o limiar)

  Respostas extras no modo GRIP:
    ANGLE <n>     telemetria enquanto fecha ou volta ao repouso
    CONTACT <n>   obstáculo detectado em n graus (só com sensor de corrente), o motor para
    LIMIT <n>     chegou ao fechamento máximo (garra fechou vazia)
    RELEASED      o homem-morto estourou e a garra voltou sozinha ao repouso
*/

#include <Servo.h>

// ---------- Pinos e ângulos ----------
const uint8_t SERVO_PIN   = A1;
const uint8_t CURRENT_PIN = A0;
// POLARIDADE: a garra começa e repousa ABERTA (ANGLE_OPEN). Segurar espaço (GRIP) leva o servo
// de ANGLE_OPEN até ANGLE_CLOSE (fecha); soltar volta para ANGLE_OPEN. Se a garra física se
// mover ao contrário, troque os dois valores (ANGLE_OPEN = 20 e ANGLE_CLOSE = 100).
const int ANGLE_OPEN  = 100;    // abertura máxima (repouso ao soltar)
const int ANGLE_CLOSE = 20;     // fechamento (fim do curso ao segurar espaço)

// ---------- Ajustes do GRIP ----------
const unsigned long STEP_MS          = 10;   // fechar: 1 grau a cada 10 ms (~0,8 s para os 80 graus de curso)
const unsigned long RETURN_STEP_MS   = 5;    // voltar ao soltar: 1 grau a cada 5 ms (~0,4 s para os 80 graus de curso)
const unsigned long GRIP_TIMEOUT_MS  = 400;  // sem GRIP por esse tempo -> volta ao repouso

// ---------- Detecção de contato ----------
const bool          USE_CURRENT_SENSOR  = false; // true só com sensor de corrente ligado em A0 (sem sensor, A0 solto "sente" contato falso e a garra para cedo)
const int           CONTACT_THRESHOLD   = 60;   // contagens ADC acima da base (calibre com CURRENT)
const unsigned long CONTACT_CONFIRM_MS  = 80;   // precisa ficar acima do limiar por esse tempo
const unsigned long IGNORE_AFTER_START  = 250;  // ignora o pico de partida do motor
const int           BACKOFF_DEG         = 2;    // recua um pouco ao detectar, aliviando a força

Servo servo;
int currentAngle = ANGLE_OPEN;

bool gripping = false;
bool contact  = false;
bool limitReported = false;
bool returning = false;     // voltando gradualmente ao repouso
unsigned long lastReturn = 0;
unsigned long lastGrip = 0, gripStart = 0, lastStep = 0, lastTelemetry = 0, overSince = 0;
int lastReportedAngle = -1;

float baseline = 0;   // leitura do sensor com o servo parado
float filtered = 0;   // leitura filtrada

char    buf[32];
uint8_t blen = 0;

void moveTo(int angle) {
  // Trava mecânica por software: nunca passa dos limites da garra (20° a 100°)
  angle = constrain(angle, min(ANGLE_CLOSE, ANGLE_OPEN), max(ANGLE_CLOSE, ANGLE_OPEN));
  servo.write(angle);
  currentAngle = angle;
}

float readAvg(uint8_t n) {
  long s = 0;
  for (uint8_t i = 0; i < n; i++) { s += analogRead(CURRENT_PIN); delay(2); }
  return (float)s / n;
}

void release() {
  gripping = false;
  contact = false;
  overSince = 0;
  returning = true;                 // volta ao repouso aos poucos (sem giro brusco)
  lastReturn = millis();
}

void handleCommand(String cmd) {
  cmd.trim();
  cmd.toUpperCase();

  if (cmd == "GRIP") {
    lastGrip = millis();
    if (!gripping) {                 // início do aperto
      gripping = true;
      contact = false;
      limitReported = false;
      returning = false;
      overSince = 0;
      gripStart = lastGrip;
      lastStep = lastGrip;
      Serial.println("OK GRIP");
    }
  } else if (cmd == "RELEASE") {
    release();
    Serial.println("OK RELEASE");
    Serial.print("ANGLE "); Serial.println(currentAngle);
  } else if (cmd == "OPEN") {
    gripping = false; contact = false; returning = false;
    moveTo(ANGLE_OPEN);
    Serial.println("OK OPEN");
  } else if (cmd == "CLOSE") {
    gripping = false; contact = false; returning = false;
    moveTo(ANGLE_CLOSE);
    Serial.println("OK CLOSE");
  } else if (cmd.startsWith("ANGLE ")) {
    gripping = false; contact = false; returning = false;
    moveTo(cmd.substring(6).toInt());
    Serial.print("OK ANGLE ");
    Serial.println(currentAngle);
  } else if (cmd == "STATUS") {
    Serial.print("STATUS ANGLE ");
    Serial.print(currentAngle);
    Serial.print(" GRIP ");
    Serial.print(gripping ? 1 : 0);
    Serial.print(" CONTACT ");
    Serial.println(contact ? 1 : 0);
  } else if (cmd == "CURRENT") {
    Serial.print("CURRENT raw=");
    Serial.print((int)filtered);
    Serial.print(" base=");
    Serial.print((int)baseline);
    Serial.print(" delta=");
    Serial.println((int)abs(filtered - baseline));
  } else if (cmd.length() > 0) {
    Serial.println("ERR comando invalido");
  }
}

// Leitura de serial sem bloquear (o loop precisa continuar rodando para mover o servo)
void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      buf[blen] = 0;
      handleCommand(String(buf));
      blen = 0;
    } else if (c != '\r' && blen < sizeof(buf) - 1) {
      buf[blen++] = c;
    }
  }
}

void setup() {
  Serial.begin(9600);
  servo.attach(SERVO_PIN);
  moveTo(ANGLE_OPEN);                 // começa no repouso (aberta): sem giro brusco no 1º aperto
  delay(600);                         // espera o servo assentar para medir a base
  baseline = readAvg(32);
  filtered = baseline;
  Serial.println("READY");
}

void loop() {
  readSerial();
  unsigned long now = millis();

  // Filtro da corrente (média móvel exponencial)
  if (USE_CURRENT_SENSOR) filtered += ((float)analogRead(CURRENT_PIN) - filtered) * 0.1f;

  // Homem-morto: parou de chegar GRIP -> volta ao repouso
  if (gripping && now - lastGrip > GRIP_TIMEOUT_MS) {
    release();
    Serial.println("RELEASED");
    Serial.print("ANGLE "); Serial.println(currentAngle);
    return;
  }

  if (gripping && !contact) {
    // Detecção de contato: corrente acima do limiar de forma sustentada
    if (USE_CURRENT_SENSOR && now - gripStart > IGNORE_AFTER_START) {
      if (abs(filtered - baseline) > CONTACT_THRESHOLD) {
        if (overSince == 0) overSince = now;
        if (now - overSince >= CONTACT_CONFIRM_MS) {
          contact = true;
          moveTo(currentAngle + BACKOFF_DEG);   // alivia a força contra o obstáculo (recua para o lado aberto)
          Serial.print("CONTACT ");
          Serial.println(currentAngle);
          return;
        }
      } else {
        overSince = 0;
      }
    }

    // Fecha aos poucos: o servo só recebe um alvo ~1 grau à frente.
    if (now - lastStep >= STEP_MS) {
      lastStep = now;
      if (currentAngle > ANGLE_CLOSE) {
        moveTo(currentAngle - 1);
      } else if (!limitReported) {
        limitReported = true;
        Serial.print("LIMIT ");
        Serial.println(currentAngle);
      }
    }

  }

  // Volta ao repouso (soltou o espaço ou o homem-morto estourou)
  if (returning && now - lastReturn >= RETURN_STEP_MS) {
    lastReturn = now;
    if (currentAngle < ANGLE_OPEN) moveTo(currentAngle + 1);
    else if (currentAngle > ANGLE_OPEN) moveTo(currentAngle - 1);
    else returning = false;
  }

  if ((gripping && !contact) || returning) {
    if (now - lastTelemetry >= 100 && currentAngle != lastReportedAngle) {
      lastTelemetry = now;
      lastReportedAngle = currentAngle;
      Serial.print("ANGLE ");
      Serial.println(currentAngle);
    }
  }
}

/*
  HARDWARE DA DETECÇÃO DE CONTATO
  Servo comum não informa posição nem força. A forma de "sentir" o objeto é medir a
  corrente: quando a mandíbula encosta e o motor trava, a corrente sobe.

  Opção A (recomendada): módulo INA219 / ACS712 (saída analógica) no fio de
  alimentação (+5 V) do servo, com a saída ligada em A0. Use fonte externa para o
  servo (GND comum com o Arduino).

  Opção B: resistor shunt de ~0,5-1 ohm no GND do servo, com o ponto entre servo e
  resistor ligado em A0 (ganho baixo, o limiar precisa ser bem ajustado).

  Calibração: abra o monitor/enviar CURRENT com a garra parada e depois durante o
  aperto contra um objeto. Ajuste CONTACT_THRESHOLD entre o "delta" em movimento livre
  e o "delta" travado. Se não tiver sensor, ponha USE_CURRENT_SENSOR = false: o GRIP
  continua funcionando, apenas sem parada automática.
*/
