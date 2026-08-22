/*
 * Telltale - MCU side.
 *
 * Owns the I2C bus and feeds Python two sensor channels over the Bridge:
 *
 *   VL53L0X  (0x29)  distance, sampled ~50 Hz -> RMS / peak-to-peak wobble  = vibration
 *   MLX90640 (0x33)  32x24 thermal frame, 1 Hz -> min / mean / max / hotspot = temperature
 *
 * Both are optional. setup() probes the bus and only drives what answered; Python is told
 * which sensors exist via sensor_status and skips any channel that never reports.
 *
 * Summaries are computed here rather than streaming raw samples: the sampling clock lives
 * on this side, and a 768-pixel thermal frame has no business crossing the Bridge.
 *
 * Python -> MCU: gauge_status(status, percent) drives the LED matrix.
 * Status codes must stay in sync with STATUS_* in python/config.py.
 */

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include <Wire.h>
#include <VL53L0X.h>
#include <Adafruit_MLX90640.h>

// ----------------------------------------------------------------- status codes
#define ST_BOOT 0
#define ST_SCANNING 1
#define ST_UNCALIBRATED 2
#define ST_OK 3
#define ST_WATCH 4
#define ST_ALARM 5

static const unsigned long BOOT_LOGO_MS = 30000;

// ----------------------------------------------------------------- sensor setup
static const uint8_t TOF_ADDR = 0x29;
static const uint8_t THERMAL_ADDR = 0x33;

// 20 ms ranging budget -> ~50 samples/s, which is the vibration sample rate.
static const uint32_t TOF_TIMING_BUDGET_US = 20000;
static const unsigned long VIB_WINDOW_MS = 1000;   // one summary per second
static const unsigned long THERMAL_PERIOD_MS = 1000;
static const unsigned long STATUS_PERIOD_MS = 5000;
static const uint8_t VIB_CAPACITY = 128;
// Fewer samples than this in a window means the window was disrupted (usually by a
// thermal frame read) and the statistics aren't worth publishing.
static const uint8_t VIB_MIN_SAMPLES = 10;

static const int THERMAL_W = 32;
static const int THERMAL_H = 24;

VL53L0X tof;
Adafruit_MLX90640 mlx;
ArduinoLEDMatrix matrix;

// The UNO Q exposes three I2C controllers to Arduino (the zephyr,user "i2cs" list):
//   Wire  = i2c@40005800   Wire1 = i2c@40008400   Wire2 = i2c@46002800
// The Qwiic connector is not necessarily the first one, and which it is isn't documented
// anywhere we could find - so probe all three and bind to whichever answers.
static TwoWire *const BUSES[] = {&Wire, &Wire1, &Wire2};
static const uint8_t NUM_BUSES = sizeof(BUSES) / sizeof(BUSES[0]);

static bool tofPresent = false;
static bool thermalPresent = false;
static int8_t tofBus = -1;
static int8_t thermalBus = -1;
static uint32_t tofFailures = 0;
static uint32_t thermalFailures = 0;

static float thermalFrame[THERMAL_W * THERMAL_H];

static uint16_t vibSamples[VIB_CAPACITY];
static uint8_t vibCount = 0;
static unsigned long vibWindowStart = 0;
static unsigned long lastThermalMs = 0;
static unsigned long lastStatusMs = 0;
static unsigned long lastProbeMs = 0;
static const unsigned long PROBE_PERIOD_MS = 5000;

// ------------------------------------------------------------------ LED matrix
static const int MATRIX_W = 13;
static const int MATRIX_H = 8;
static uint8_t frameBuf[MATRIX_W * MATRIX_H];

static volatile int gStatus = ST_BOOT;
static volatile int gPercent = 0;
static unsigned long gLastStatusMs = 0;

static const uint8_t GLYPH_CHECK[5] = {0b00001, 0b00010, 0b10100, 0b01000, 0b00000};
static const uint8_t GLYPH_BANG[5]  = {0b00100, 0b00100, 0b00100, 0b00000, 0b00100};
static const uint8_t GLYPH_CROSS[5] = {0b10001, 0b01010, 0b00100, 0b01010, 0b10001};
static const uint8_t GLYPH_QUERY[5] = {0b01110, 0b10001, 0b00010, 0b00100, 0b00100};

static void clearFrame() {
  memset(frameBuf, 0, sizeof(frameBuf));
}

static inline void setPixel(int x, int y, uint8_t level) {
  if (x < 0 || x >= MATRIX_W || y < 0 || y >= MATRIX_H) return;
  frameBuf[y * MATRIX_W + x] = level;
}

static void drawGlyph(const uint8_t rows[5], int originX, int originY, uint8_t level) {
  for (int y = 0; y < 5; y++) {
    for (int x = 0; x < 5; x++) {
      if (rows[y] & (1 << (4 - x))) setPixel(originX + x, originY + y, level);
    }
  }
}

static void drawBar(int percent, uint8_t level) {
  if (percent < 0) percent = 0;
  if (percent > 100) percent = 100;
  int lit = (percent * MATRIX_W + 50) / 100;
  for (int x = 0; x < MATRIX_W; x++) {
    uint8_t v = (x < lit) ? level : 1;
    setPixel(x, MATRIX_H - 2, v);
    setPixel(x, MATRIX_H - 1, v);
  }
}

static void drawScanning(unsigned long now) {
  static const int8_t ORBIT[12][2] = {{2, 0}, {3, 0}, {4, 1}, {4, 2}, {4, 3}, {3, 4},
                                      {2, 4}, {1, 4}, {0, 3}, {0, 2}, {0, 1}, {1, 0}};
  int step = (now / 90) % 12;
  for (int i = 0; i < 12; i++) {
    int age = (step - i + 12) % 12;
    uint8_t level = (age == 0) ? 7 : (age == 1) ? 3 : (age == 2) ? 1 : 0;
    if (level) setPixel(4 + ORBIT[i][0], 1 + ORBIT[i][1], level);
  }
}

static void render() {
  unsigned long now = millis();
  if (now < BOOT_LOGO_MS) return;

  int status = gStatus;
  int percent = gPercent;
  if (gLastStatusMs != 0 && (now - gLastStatusMs) > 30000UL) status = ST_SCANNING;

  bool blinkSlow = ((now / 500) % 2) == 0;
  bool blinkFast = ((now / 200) % 2) == 0;

  clearFrame();
  switch (status) {
    case ST_OK:
      drawGlyph(GLYPH_CHECK, 4, 1, 4);
      drawBar(percent, 4);
      break;
    case ST_WATCH:
      drawGlyph(GLYPH_BANG, 4, 1, blinkSlow ? 7 : 2);
      drawBar(percent, 5);
      break;
    case ST_ALARM:
      drawGlyph(GLYPH_CROSS, 4, 1, blinkFast ? 7 : 1);
      drawBar(percent, blinkFast ? 7 : 2);
      break;
    case ST_UNCALIBRATED:
      drawGlyph(GLYPH_QUERY, 4, 1, blinkSlow ? 5 : 2);
      break;
    case ST_SCANNING:
    case ST_BOOT:
    default:
      drawScanning(now);
      break;
  }
  matrix.draw(frameBuf);
}

// --------------------------------------------------------------------- sensors
static bool i2cResponds(TwoWire *bus, uint8_t address) {
  bus->beginTransmission(address);
  return bus->endTransmission() == 0;
}

// Which bus has something at this address, or -1.
static int8_t findBusWith(uint8_t address) {
  for (uint8_t i = 0; i < NUM_BUSES; i++) {
    if (i2cResponds(BUSES[i], address)) return (int8_t)i;
  }
  return -1;
}

// Walk every bus and report what answered. "Nothing at 0x29" is a dead end when you're
// debugging a hand-soldered connector; knowing that 0x29 turned up on Wire1, or that no bus
// has anything on it at all, is the actual diagnosis.
// Cached so publishStatus() can re-send it. The MCU boots before the Python app finishes
// registering its Bridge handlers, so the scan sent during setup() is usually shouted into
// an empty room.
static uint8_t scanFound[NUM_BUSES][4];
static uint8_t scanCount[NUM_BUSES];
static bool scanValid = false;

static void notifyScan(uint8_t b) {
  Bridge.notify("i2c_scan", (int)b, (int)scanCount[b],
                (int)scanFound[b][0], (int)scanFound[b][1],
                (int)scanFound[b][2], (int)scanFound[b][3]);
}

static void scanAndReport() {
  for (uint8_t b = 0; b < NUM_BUSES; b++) {
    uint8_t found[8];
    uint8_t n = 0;
    Serial.print("I2C scan Wire");
    if (b) Serial.print(b);
    Serial.print(":");
    for (uint8_t addr = 0x08; addr < 0x78; addr++) {
      if (!i2cResponds(BUSES[b], addr)) continue;
      Serial.print(" 0x");
      Serial.print(addr, HEX);
      if (n < 8) found[n] = addr;
      n++;
    }
    if (n == 0) Serial.print(" nothing responded");
    Serial.println();

    scanCount[b] = n;
    for (uint8_t i = 0; i < 4; i++) scanFound[b][i] = (i < n && i < 8) ? found[i] : 0;
    notifyScan(b);
  }
  scanValid = true;
}

static void setupToF(bool verbose) {
  int8_t bus = findBusWith(TOF_ADDR);
  if (bus < 0) {
    if (verbose) Serial.println("VL53L0X not found at 0x29 on any bus - vibration disabled");
    return;
  }
  tof.setBus(BUSES[bus]);
  tof.setTimeout(500);
  if (!tof.init()) {
    Serial.print("VL53L0X answered at 0x29 on Wire");
    Serial.print(bus);
    Serial.println(" but init() failed - check supply voltage");
    return;
  }
  tof.setMeasurementTimingBudget(TOF_TIMING_BUDGET_US);
  tof.startContinuous(0);  // back-to-back ranging
  tofPresent = true;
  tofBus = bus;
  vibWindowStart = millis();
  vibCount = 0;
  Serial.print("VL53L0X ready on Wire");
  Serial.print(bus);
  Serial.println(" - vibration channel active");
}

static void setupThermal(bool verbose) {
  int8_t bus = findBusWith(THERMAL_ADDR);
  if (bus < 0) {
    if (verbose) Serial.println("MLX90640 not found at 0x33 on any bus - temperature disabled");
    return;
  }
  if (!mlx.begin(THERMAL_ADDR, BUSES[bus])) {
    Serial.println("MLX90640 answered at 0x33 but begin() failed");
    return;
  }
  thermalBus = bus;
  mlx.setMode(MLX90640_CHESS);
  mlx.setResolution(MLX90640_ADC_18BIT);
  // 16 Hz: a full frame is two subpages, so getFrame() blocks ~125 ms. Slower refresh
  // rates would stall the time-of-flight sampling for most of a second.
  mlx.setRefreshRate(MLX90640_16_HZ);
  thermalPresent = true;
  Serial.print("MLX90640 ready on Wire");
  Serial.print(thermalBus);
  Serial.println(" - temperature channel active");
}

static void publishVibration(unsigned long now) {
  unsigned long elapsed = now - vibWindowStart;
  vibWindowStart = now;
  uint8_t n = vibCount;
  vibCount = 0;

  if (n < VIB_MIN_SAMPLES || elapsed == 0) return;

  float windowS = elapsed / 1000.0f;
  float sum = 0.0f;
  uint16_t lo = 0xFFFF, hi = 0;
  for (uint8_t i = 0; i < n; i++) {
    sum += vibSamples[i];
    if (vibSamples[i] < lo) lo = vibSamples[i];
    if (vibSamples[i] > hi) hi = vibSamples[i];
  }
  float mean = sum / n;

  float sumSq = 0.0f;
  uint16_t crossings = 0;
  float previous = vibSamples[0] - mean;
  for (uint8_t i = 0; i < n; i++) {
    float deviation = vibSamples[i] - mean;
    sumSq += deviation * deviation;
    if (i > 0 && ((deviation < 0.0f) != (previous < 0.0f))) crossings++;
    previous = deviation;
  }
  float rms = sqrtf(sumSq / n);
  // Crude but cheap dominant-frequency estimate: a sine crosses its mean twice a cycle.
  float dominantHz = (crossings / 2.0f) / windowS;

  Bridge.notify("tof_summary", mean, rms, (float)(hi - lo), dominantHz, (int)n);
}

static void publishThermal() {
  if (mlx.getFrame(thermalFrame) != 0) {
    thermalFailures++;
    return;
  }
  float lo = thermalFrame[0], hi = thermalFrame[0], sum = 0.0f;
  int hotIndex = 0;
  for (int i = 0; i < THERMAL_W * THERMAL_H; i++) {
    float v = thermalFrame[i];
    sum += v;
    if (v < lo) lo = v;
    if (v > hi) { hi = v; hotIndex = i; }
  }
  float mean = sum / (THERMAL_W * THERMAL_H);
  // The coldest pixel stands in for the background the hot spot is judged against; the
  // Python side's "delta_ambient" metric is max - this.
  Bridge.notify("thermal_stats", lo, mean, hi, lo,
                (int)(hotIndex % THERMAL_W), (int)(hotIndex / THERMAL_W));
}

static void publishStatus() {
  Bridge.notify("sensor_status", (int)tofPresent, (int)thermalPresent,
                (int)tofFailures, (int)thermalFailures);
  // Re-send the cached bus scan: cheap, and it survives the app restarting without the MCU.
  if (scanValid) {
    for (uint8_t b = 0; b < NUM_BUSES; b++) notifyScan(b);
  }
}

// Re-probe for anything still missing, so a sensor plugged in after boot is picked up
// without a restart. Once everything has been found this stops costing anything.
static void maintainSensors(unsigned long now) {
  if (tofPresent && thermalPresent) return;
  if (now - lastProbeMs < PROBE_PERIOD_MS) return;
  lastProbeMs = now;
  // Full bus scan only while nothing at all has been found. Once a sensor is live, a scan
  // would stall its sampling for no benefit - just poke the address that's still missing.
  if (!tofPresent && !thermalPresent) scanAndReport();
  if (!tofPresent) setupToF(false);
  if (!thermalPresent) setupThermal(false);
}

// Bridge handler: Python side is Bridge.notify("gauge_status", status, percent).
void onGaugeStatus(int status, int percent) {
  gStatus = status;
  gPercent = percent;
  gLastStatusMs = millis();
}

void setup() {
  Serial.begin(115200);
  Bridge.begin();
  Bridge.provide("gauge_status", onGaugeStatus);

  matrix.begin();
  matrix.setGrayscaleBits(3);
  clearFrame();

  // Bring up every exposed I2C controller - we don't know which one the Qwiic connector
  // is wired to, and an unused bus costs nothing.
  for (uint8_t i = 0; i < NUM_BUSES; i++) {
    BUSES[i]->begin();
    BUSES[i]->setClock(400000);
  }
  scanAndReport();
  setupToF(true);
  setupThermal(true);

  lastProbeMs = millis();
  vibWindowStart = millis();
  Serial.println("Telltale MCU ready");
}

void loop() {
  unsigned long now = millis();

  if (tofPresent) {
    // Blocks for about one ranging period, which is what paces this loop.
    uint16_t mm = tof.readRangeContinuousMillimeters();
    if (tof.timeoutOccurred()) {
      tofFailures++;
    } else if (mm > 0 && mm < 8190 && vibCount < VIB_CAPACITY) {
      vibSamples[vibCount++] = mm;
    }
  }

  now = millis();
  if (now - vibWindowStart >= VIB_WINDOW_MS) {
    if (tofPresent) publishVibration(now);
    else vibWindowStart = now;

    // Read the thermal frame right after publishing, so the ~125 ms stall lands at the
    // start of a vibration window instead of chopping one in half.
    if (thermalPresent && (now - lastThermalMs) >= THERMAL_PERIOD_MS) {
      publishThermal();
      lastThermalMs = millis();
      vibWindowStart = millis();
      vibCount = 0;
    }
  }

  maintainSensors(now);

  if (now - lastStatusMs >= STATUS_PERIOD_MS) {
    publishStatus();
    lastStatusMs = now;
  }

  render();
  if (!tofPresent) delay(20);  // nothing is pacing the loop without the rangefinder
}
