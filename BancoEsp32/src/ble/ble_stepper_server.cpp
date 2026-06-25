#include "ble_stepper_server.h"

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ----- Pin configuration -----
#define ROT_STEP_PIN   7    // Rotational motor STEP
#define LIN_STEP_PIN   9    // Linear motor STEP
#define DIR_PIN        21   // Shared direction pin
#define SLEEP_PIN      20   // Shared sleep/enable pin (active HIGH)
#define POSITION_PIN    0   // Position input (digital, weak pull-up)
#define FAN_PIN        10   // Fan control pin (active LOW)

// ----- Timing -----
#define PULSE_WIDTH_US    1000  // 1 ms HIGH pulse
#define PULSE_INTERVAL_US_DEFAULT 5000  // 5 ms between pulse starts (4 ms LOW after pulse)

// ----- Motor steps -----
#define LINEAR_STEPS      1000
#define ROTATIONAL_STEPS  1085  // 5370 / 5 (1/5 revolution)

// ----- BLE UUIDs (custom 128-bit) -----
#define BLE_SERVICE_UUID     "AA000001-1234-1234-1234-1234567890AA"
#define BLE_CMD_CHAR_UUID    "AA000002-1234-1234-1234-1234567890AA"
#define BLE_STATUS_CHAR_UUID "AA000003-1234-1234-1234-1234567890AA"

// ----- Global state -----
static volatile bool     motorBusy      = false;
static volatile bool     stopRequested  = false;
static volatile uint32_t pulseIntervalUs = PULSE_INTERVAL_US_DEFAULT;
static BLECharacteristic* statusCharacteristic = nullptr;

// ----- Task parameter struct -----
struct MotorTaskParams {
    uint8_t  stepPin;
    bool     dirHigh;  // true → DIR HIGH, false → DIR LOW
    uint16_t steps;
};

// ============================================================
//  GPIO layer
// ============================================================

static void setupMotorGpio() {
    pinMode(ROT_STEP_PIN,  OUTPUT);
    pinMode(LIN_STEP_PIN,  OUTPUT);
    pinMode(DIR_PIN,       OUTPUT);
    pinMode(SLEEP_PIN,    OUTPUT);
    pinMode(POSITION_PIN, INPUT_PULLUP);
    pinMode(FAN_PIN,      OUTPUT);

    digitalWrite(ROT_STEP_PIN, LOW);
    digitalWrite(LIN_STEP_PIN, LOW);
    digitalWrite(DIR_PIN,      LOW);
    digitalWrite(SLEEP_PIN,    LOW);   // active HIGH → LOW = disabled
    digitalWrite(FAN_PIN,     LOW);    // active HIGH → LOW = disabled

    Serial.println("Motor GPIO initialized:");
    Serial.println("ROT_STEP=GPIO7, LIN_STEP=GPIO9, DIR=GPIO21, SLEEP=GPIO20, POSITION=GPIO0, FAN=GPIO10");
}

static void generateStepPulse(uint8_t stepPin) {
    digitalWrite(stepPin, HIGH);
    delayMicroseconds(PULSE_WIDTH_US);
    digitalWrite(stepPin, LOW);
}

static void enableMotors()  { digitalWrite(SLEEP_PIN, HIGH); }
static void disableMotors() { digitalWrite(SLEEP_PIN, LOW);  }

// ============================================================
//  Motor task (FreeRTOS — replaces Python threads)
// ============================================================

static void motorTask(void* pvParams) {
    auto* params = static_cast<MotorTaskParams*>(pvParams);

    enableMotors();
    delayMicroseconds(1000);  // A4988 wake-up time after SLEEP→HIGH
    digitalWrite(DIR_PIN, params->dirHigh ? HIGH : LOW);
    delayMicroseconds(1);     // DIR setup time before first STEP (A4988 requires ≥200 ns)

    bool stopped = false;
    for (uint16_t i = 0; i < params->steps; i++) {
        if (stopRequested) {
            stopped = true;
            break;
        }
        generateStepPulse(params->stepPin);
        delayMicroseconds(pulseIntervalUs - PULSE_WIDTH_US);
    }

    disableMotors();

    stopRequested = false;

    if (statusCharacteristic) {
        statusCharacteristic->setValue(stopped ? "STOPPED" : "COMPLETE");
        statusCharacteristic->notify();
    }

    motorBusy = false;
    delete params;
    vTaskDelete(NULL);
}

static void launchMotorTask(uint8_t stepPin, bool dirHigh, uint16_t steps) {
    auto* params = new MotorTaskParams{stepPin, dirHigh, steps};
    motorBusy = true;
    xTaskCreate(motorTask, "motorTask", 2048, params, 1, NULL);
}

// ============================================================
//  BLE helpers
// ============================================================

static void sendStatus(const char* msg) {
    if (statusCharacteristic) {
        statusCharacteristic->setValue(msg);
        statusCharacteristic->notify();
    }
}

// ============================================================
//  BLE callbacks
// ============================================================

class CommandCallback : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic* characteristic) override {
        String cmd = characteristic->getValue().c_str();
        cmd.trim();
        cmd.toUpperCase();

        Serial.printf("Received command: %s\n", cmd.c_str());

        // STOP, SETINTERVAL, GETPOSITION, FANON and FANOFF bypass the busy guard intentionally
        if (cmd == "STOP") {
            stopRequested = true;
            sendStatus("OK");
            return;
        }

        if (cmd.startsWith("SETINTERVAL:")) {
            int32_t val = cmd.substring(12).toInt();
            if (val > (int32_t)PULSE_WIDTH_US) {
                pulseIntervalUs = (uint32_t)val;
                Serial.printf("Pulse interval set to %u us\n", pulseIntervalUs);
                sendStatus("OK");
            } else {
                Serial.printf("SETINTERVAL rejected: %d (must be > %d)\n", val, PULSE_WIDTH_US);
                sendStatus("INVALID");
            }
            return;
        }

        if (cmd == "GETPOSITION") {
            //int level = digitalRead(POSITION_PIN);
            //sendStatus(level == LOW ? "ERROR" : "OK");
            sendStatus("OK");
            return;
        }

        if (cmd == "FANON") {
            digitalWrite(FAN_PIN, HIGH);
            sendStatus("OK");
            return;
        }

        if (cmd == "FANOFF") {
            digitalWrite(FAN_PIN, LOW);
            sendStatus("OK");
            return;
        }
        //

        if (motorBusy) {
            Serial.println("Motor busy — sending WAIT");
            sendStatus("WAIT");
            return;
        }

        // Movement commands, only accepted if motor is not busy.

        if (cmd == "MOVEUP") {
            sendStatus("OK");
            launchMotorTask(LIN_STEP_PIN, false, LINEAR_STEPS);      // DIR LOW  = UP
        } else if (cmd == "MOVEDOWN") {
            sendStatus("OK");
            launchMotorTask(LIN_STEP_PIN, true,  LINEAR_STEPS);      // DIR HIGH = DOWN
        } else if (cmd == "MOVECLOCKWISE") {
            sendStatus("OK");
            launchMotorTask(ROT_STEP_PIN, false, ROTATIONAL_STEPS);  // DIR LOW  = CW
        } else if (cmd == "MOVECOUNTERCLOCKWISE") {
            sendStatus("OK");
            launchMotorTask(ROT_STEP_PIN, true,  ROTATIONAL_STEPS);  // DIR HIGH = CCW
        } else {
            Serial.printf("Unknown command: %s\n", cmd.c_str());
            sendStatus("INVALID");
        }
    }
};

class ConnectionCallback : public BLEServerCallbacks {
    void onConnect(BLEServer* server) override {
        Serial.println("Client connected.");
    }

    void onDisconnect(BLEServer* server) override {
        Serial.println("Client disconnected. Restarting advertising...");
        BLEDevice::startAdvertising();
    }
};

// ============================================================
//  Public init
// ============================================================

void initBleStepperServer() {
    setupMotorGpio();

    BLEDevice::init("ESP32_STEPPER");

    BLEServer* bleServer = BLEDevice::createServer();
    bleServer->setCallbacks(new ConnectionCallback());

    BLEService* service = bleServer->createService(BLE_SERVICE_UUID);

    // Command characteristic — client writes commands
    BLECharacteristic* cmdCharacteristic = service->createCharacteristic(
        BLE_CMD_CHAR_UUID,
        BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR
    );
    cmdCharacteristic->setCallbacks(new CommandCallback());

    // Status characteristic — server notifies client (OK / WAIT / COMPLETE / INVALID)
    statusCharacteristic = service->createCharacteristic(
        BLE_STATUS_CHAR_UUID,
        BLECharacteristic::PROPERTY_NOTIFY
    );
    statusCharacteristic->addDescriptor(new BLE2902());

    service->start();

    BLEAdvertising* advertising = BLEDevice::getAdvertising();
    advertising->addServiceUUID(BLE_SERVICE_UUID);
    advertising->setScanResponse(true);
    BLEDevice::startAdvertising();

    Serial.println("BLE server started. Device name: ESP32_STEPPER");
    Serial.println("Commands: MOVEUP / MOVEDOWN / MOVECLOCKWISE / MOVECOUNTERCLOCKWISE / GETPOSITION / FANON / FANOFF / STOP / SETINTERVAL:<us>");
}
