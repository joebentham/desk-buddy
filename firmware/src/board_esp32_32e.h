#pragma once
#include <ctype.h>

// 3.2 inch 240x320 resistive-touch ESP32-32E board. See docs/esp32-32e.md.
constexpr int PIN_SCLK = 14, PIN_MOSI = 13, PIN_MISO = 12;
constexpr int PIN_CS = 15, PIN_DC = 2, PIN_RST = -1, PIN_BL = 27;
constexpr int PIN_TOUCH_CS = 33, PIN_TOUCH_IRQ = 36;
// This panel's native CASET/RASET range is 320x240, unlike the driver's
// default 240x320. Offset 5 selects the MADCTL MV orientation
// while exposing a 240x320 canvas at user rotation 0 (portrait, USB-C top).
// INVON/RGB are the panel defaults; the asymmetric native-window readback
// below checks the address mapping, independently of optical colour order.
struct PanelSettings {
  bool invert, bgr;
  uint8_t rotation;
};
constexpr PanelSettings BOARD_PANEL_DEFAULTS = {true, false, 0};

// Accept exactly three single-digit values, with whitespace between them.
// Validate everything before changing the live panel; never save to NVS.
static bool parsePanelSettings(const char* p, PanelSettings& out) {
  uint8_t values[3];
  for (unsigned i = 0; i < 3; ++i) {
    while (*p && isspace(static_cast<unsigned char>(*p))) ++p;
    if (*p < '0' || *p > (i == 2 ? '7' : '1')) return false;
    values[i] = *p++ - '0';
    if (*p && !isspace(static_cast<unsigned char>(*p))) return false;
  }
  while (*p && isspace(static_cast<unsigned char>(*p))) ++p;
  if (*p) return false;
  out = {bool(values[0]), bool(values[1]), values[2]};
  return true;
}

// Read complete wire responses slowly before the SPI peripheral owns the pins.
// Includes dummy bits/bytes so controller evidence does not depend on a library
// read-index convention. No reset or display-memory write is involved.
static void probePanelGPIO() {
  for (int pin : {PIN_SCLK, PIN_MOSI, PIN_CS, PIN_DC}) pinMode(pin, OUTPUT);
  pinMode(PIN_MISO, INPUT);
  digitalWrite(PIN_CS, HIGH); digitalWrite(PIN_SCLK, LOW);
  auto clockBit = [](bool output) {
    digitalWrite(PIN_MOSI, output); delayMicroseconds(5);
    digitalWrite(PIN_SCLK, HIGH); delayMicroseconds(5);
    int value = digitalRead(PIN_MISO);
    digitalWrite(PIN_SCLK, LOW); delayMicroseconds(5);
    return value;
  };
  for (uint8_t command : {0x04, 0xD3, 0xDA, 0xDB, 0xDC}) {
    digitalWrite(PIN_DC, LOW); digitalWrite(PIN_CS, LOW); delayMicroseconds(5);
    for (int bit = 7; bit >= 0; --bit) clockBit(command & (1 << bit));
    digitalWrite(PIN_DC, HIGH); delayMicroseconds(5);
    Serial.printf("panel GPIO raw %02X:", unsigned(command));
    for (int byte = 0; byte < 5; ++byte) {
      uint8_t value = 0;
      for (int bit = 0; bit < 8; ++bit) value = (value << 1) | clockBit(false);
      Serial.printf(" %02X", unsigned(value));
    }
    digitalWrite(PIN_CS, HIGH); Serial.println();
  }
}

class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_ILI9341 panel;
  lgfx::Bus_SPI bus;
 public:
  LGFX() {
    auto b = bus.config();
    b.spi_host = SPI2_HOST; b.spi_mode = 0;
    b.freq_write = 20000000; b.freq_read = 500000;
    b.pin_sclk = PIN_SCLK; b.pin_mosi = PIN_MOSI; b.pin_miso = PIN_MISO; b.pin_dc = PIN_DC;
    b.spi_3wire = false; b.use_lock = true; b.dma_channel = SPI_DMA_CH_AUTO;
    bus.config(b); panel.setBus(&bus);
    auto p = panel.config();
    p.pin_cs = PIN_CS; p.pin_rst = PIN_RST; p.pin_busy = -1;
    p.panel_width = p.memory_width = 320;
    p.panel_height = p.memory_height = 240;
    p.offset_rotation = 5;
    p.invert = BOARD_PANEL_DEFAULTS.invert;
    p.rgb_order = !BOARD_PANEL_DEFAULTS.bgr; // LovyanGFX true = RGB, not BGR
    p.readable = true; p.bus_shared = true;
    p.dummy_read_bits = 0;
    panel.config(p); setPanel(&panel);
  }

  void tunePanel(const PanelSettings& settings) {
    auto p = panel.config();
    p.invert = settings.invert; p.rgb_order = !settings.bgr;
    panel.config(p);
    invertDisplay(false); // effective INVON = false XOR p.invert
    setRotation(settings.rotation); // also refreshes MADCTL and clipping bounds
    clearGRAM();
  }

  // Explicit native window: independent of user rotation, clipping and the
  // library's cached window. Always overwrite all 76,800 physical pixels.
  void clearGRAM() {
    startWrite();
    writeCommand(0x36); writeData(panel.config().rgb_order ? 0x00 : 0x08);
    rawWindow(0, 0, 319, 239);
    writeCommand(0x2C); panel.writeBlock(0, 320 * 240);
    endWrite();
    setRotation(getRotation()); // restore MADCTL, invalidate cached window
  }

  void rawWindow(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1) {
    writeCommand(0x2A); writeData16(x0); writeData16(x1);
    writeCommand(0x2B); writeData16(y0); writeData16(y1);
  }

  uint16_t readNativePixel(uint16_t x, uint16_t y) {
    startWrite();
    writeCommand(0x36); writeData(0x00); // native axes, no MX/MY/MV/BGR
    rawWindow(x, y, x, y);
    // RAMRD has one dummy byte then RGB666, packed little-endian by LGFX.
    uint32_t wire = panel.readCommand(0x2E, 1, 3);
    endWrite();
    setRotation(getRotation());
    return lgfx::color565(wire & 0xFF, (wire >> 8) & 0xFF, (wire >> 16) & 0xFF);
  }

  void panelEvidence() {
    Serial.printf("panel ILI9341: RDDID=%08lX D3=%08lX MADCTL=%08lX rotation=%u %dx%d BL=%d\n",
                  (unsigned long)panel.readCommand(0x04, 0, 4), (unsigned long)panel.readCommand(0xD3, 0, 4),
                  (unsigned long)panel.readCommand(0x0B, 0, 1), unsigned(getRotation()), int(width()), int(height()), PIN_BL);
    Serial.printf("panel ID1/2/3=%02lX/%02lX/%02lX status=%08lX\n",
                  (unsigned long)panel.readCommand(0xDA, 0, 1), (unsigned long)panel.readCommand(0xDB, 0, 1),
                  (unsigned long)panel.readCommand(0xDC, 0, 1), (unsigned long)panel.readCommand(0x09, 0, 4));
    const uint16_t colours[] = {TFT_RED, TFT_GREEN, TFT_BLUE, TFT_WHITE, TFT_BLACK};
    bool good = true;
    for (unsigned i = 0; i < 5; ++i) {
      drawPixel(8 + i, 8, colours[i]);
      uint16_t got = readPixel(8 + i, 8);
      Serial.printf("panel pixel %04X -> %04X\n", unsigned(colours[i]), unsigned(got));
      good &= got == colours[i];
    }
    Serial.printf("panel readback %s\n", good ? "PASS" : "FAIL (inspect controller/pins)");
    // Default MADCTL (0x20), with the full portrait canvas in bounds.
    // Native raw reads bypass readPixel's logical clipping and rotation path.
    const uint16_t points[][3] = {
      {0, 0, TFT_RED}, {239, 0, TFT_GREEN}, {0, 319, TFT_BLUE},
      {239, 319, TFT_WHITE}, {7, 240, 0xA55A}, {17, 293, 0x5AA5}
    };
    for (const auto& p : points) drawPixel(p[0], p[1], p[2]);
    good = width() == 240 && height() == 320 && panel.readCommand(0x0B, 0, 1) == 0x20;
    for (const auto& p : points) {
      uint16_t got = readNativePixel(p[1], p[0]);
      Serial.printf("panel map logical=%u,%u native=%u,%u %04X -> %04X\n",
                    p[0], p[1], p[1], p[0], p[2], got);
      good &= got == p[2];
    }
    Serial.printf("panel native mapping %s (6/6 required, native 320x240, MV=1)\n", good ? "PASS" : "FAIL");
    clearGRAM();
    good = true;
    for (const auto& p : points) good &= readNativePixel(p[1], p[0]) == TFT_BLACK;
    Serial.printf("panel full clear %s (76800 pixels, 6 native samples)\n", good ? "PASS" : "FAIL");
  }

  uint16_t rawTouch(int32_t* x, int32_t* y) {
    // Direct XPT2046 differential 12-bit conversions. No XY calibration is
    // required: every point is a tap. IRQ is observed, but not used as a gate.
    // Keep pressure in raw ADC units instead of LovyanGFX's 8-bit touch size.
    // LovyanGFX 1.3.0 rounds its TX register copy up to a whole word. Provide
    // initialized padding for that copy, but keep the XPT2046 wire length 33.
    constexpr size_t wireBytes = 33;
    uint8_t data[36] = {};
    for (unsigned i = 0; i < 32; i += 8) {
      data[i] = 0x91; data[i+1] = 0; data[i+2] = 0xB1; data[i+3] = 0;
      data[i+4] = 0xD1; data[i+5] = 0; data[i+6] = 0xC1; data[i+7] = 0;
    }
    data[32] = 0x80;  // ADC off, PENIRQ enabled after the last sample
    lgfx::spi::beginTransaction(SPI2_HOST, 1000000, 0);
    lgfx::gpio_lo(PIN_TOUCH_CS);
    lgfx::spi::readBytes(SPI2_HOST, data, wireBytes);
    lgfx::gpio_hi(PIN_TOUCH_CS);
    lgfx::spi::endTransaction(SPI2_HOST);
    uint16_t z[3], xs[3], ys[3];
    for (unsigned i = 0; i < 3; ++i) {
      const uint8_t* d = data + (i + 1) * 8; // discard settling sample
      ys[i] = (d[1] << 8 | d[2]) >> 3;
      xs[i] = (d[5] << 8 | d[6]) >> 3;
      uint16_t z1 = (d[3] << 8 | d[4]) >> 3, z2 = (d[7] << 8 | d[8]) >> 3;
      z[i] = (xs[i] > 100 && xs[i] < 4000 && ys[i] > 100 && ys[i] < 4000 && z1 > 0 && z2 < 4095)
             ? min(4095, int(z1) + 4095 - int(z2)) : 0;
    }
    std::sort(z, z + 3); std::sort(xs, xs + 3); std::sort(ys, ys + 3);
    *x = xs[1]; *y = ys[1];
    return z[1];
  }
};
