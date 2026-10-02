/*
  Controle de Servo via USB (Serial)
  Servo - sinal -> A1

  Comandos (enviar com \n no final):
    OPEN        -> abre o servo
    CLOSE       -> fecha o servo
    ANGLE <n>   -> move para n graus (0-180)
    STATUS      -> retorna a posição atual
*/

#include <Servo.h>

const uint8_t SERVO_PIN = A1;   // Servo no pino A1
const int ANGLE_OPEN  = 90;     // ajuste conforme seu mecanismo
const int ANGLE_CLOSE = 0;

Servo servo;
int currentAngle = ANGLE_CLOSE;

void moveTo(int angle) {
  angle = constrain(angle, 0, 180);
  servo.write(angle);
  currentAngle = angle;
}

void setup() {
  Serial.begin(9600);
  servo.attach(SERVO_PIN);
  moveTo(ANGLE_CLOSE);
  Serial.println("READY");
}

void loop() {
  if (!Serial.available()) return;

  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  cmd.toUpperCase();

  if (cmd == "OPEN") {
    moveTo(ANGLE_OPEN);
    Serial.println("OK OPEN");
  } else if (cmd == "CLOSE") {
    moveTo(ANGLE_CLOSE);
    Serial.println("OK CLOSE");
  } else if (cmd.startsWith("ANGLE ")) {
    moveTo(cmd.substring(6).toInt());
    Serial.print("OK ANGLE ");
    Serial.println(currentAngle);
  } else if (cmd == "STATUS") {
    Serial.print("ANGLE ");
    Serial.println(currentAngle);
  } else if (cmd.length() > 0) {
    Serial.println("ERR comando invalido");
  }
}
