#pragma once
#include "Arduino.h"
constexpr int SPI2_HOST = 1, SPI_DMA_CH_AUTO = 1;
constexpr uint16_t TFT_RED = 0xF800, TFT_GREEN = 0x07E0, TFT_BLUE = 0x001F, TFT_WHITE = 0xFFFF, TFT_BLACK = 0;
constexpr int top_left = 0, middle_center = 1;
namespace lgfx {
inline uint16_t color565(int r, int g, int b) { return (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3); }
struct Config {
  int spi_host, spi_mode, freq_write, freq_read, pin_sclk, pin_mosi, pin_miso, pin_dc, dma_channel;
  int pin_cs, pin_rst, pin_busy, panel_width, memory_width, panel_height, memory_height, dummy_read_bits, offset_rotation;
  bool spi_3wire, use_lock, invert, rgb_order, readable, bus_shared;
};
struct Bus_SPI { Config config() { return {}; } void config(Config) {} };
struct Panel_ILI9341 {
  Config state{};
  int rotation = 0, madctl = 0, command = 0, xs = 0, ys = 0, xe = 0, ye = 0;
  std::vector<uint8_t> args;
  std::vector<uint16_t>* gram = nullptr;
  void setBus(Bus_SPI*) {} Config config() { return state; } void config(Config c) { state = c; }
  int internalRotation() const {
    return ((rotation + state.offset_rotation) & 3) | ((rotation & 4) ^ (state.offset_rotation & 4));
  }
  void setRotation(int r) {
    rotation = r;
    const uint8_t orientation[] = {0, 0x64, 0xD4, 0xB0, 0x90, 0x20, 0x44, 0xF4};
    madctl = orientation[internalRotation()] | (state.rgb_order ? 0 : 8);
  }
  // Fixed hardware bounds, independent of config: catches the old transpose.
  int address(int x, int y) const {
    if (madctl & 0x20) std::swap(x, y);
    if (madctl & 0x40) x = 319-x;
    if (madctl & 0x80) y = 239-y;
    return x < 0 || x >= 320 || y < 0 || y >= 240 ? -1 : y*320+x;
  }
  void writeCommand(int cmd) { command = cmd; args.clear(); }
  void writeData(uint8_t v) {
    args.push_back(v);
    if (command == 0x36) madctl = v;
    if (args.size() == 4) {
      int start = args[0]*256+args[1], end = args[2]*256+args[3];
      if (command == 0x2A) { xs = start; xe = end; }
      if (command == 0x2B) { ys = start; ye = end; }
    }
  }
  void writeBlock(uint32_t c, uint32_t len) {
    assert(command == 0x2C && len == unsigned((xe-xs+1)*(ye-ys+1)));
    for (int y = ys; y <= ye; ++y) for (int x = xs; x <= xe; ++x) {
      int i = address(x, y); if (i >= 0) gram->at(i) = c;
    }
  }
  unsigned long readCommand(int cmd, int, int) {
    if (cmd == 0x0B) return madctl;
    if (cmd != 0x2E) return 0;
    int i = address(xs, ys);
    uint16_t c = i >= 0 ? gram->at(i) : 0;
    return ((c >> 11) << 3) | (((c >> 5) & 63) << 10) | ((c & 31) << 19);
  }
};
struct Panel_ST7789 : Panel_ILI9341 {};
struct DrawOp { std::string name, text; int x, y, w, h; };
struct LGFX_Device {
  Panel_ILI9341* hostPanel = nullptr;
  int rotation = 0;
  bool inverted = false;
  int textSize = 1, textDatum = top_left;
  int canvasWidth = 0, canvasHeight = 0;
  std::vector<DrawOp> ops;
  std::vector<uint16_t> pixels = std::vector<uint16_t>(320 * 240);
  void setPanel(Panel_ILI9341* p) { hostPanel = p; p->gram = &pixels; }
  void init() { assert(!hostBacklight.empty()); }
  void setRotation(int r) { rotation = r; hostPanel->setRotation(r); }
  void invertDisplay(bool i) { inverted = i ^ hostPanel->state.invert; }
  int getRotation() { return rotation; }
  int width() { return !hostPanel ? canvasWidth : hostPanel->internalRotation() & 1 ? hostPanel->state.panel_height : hostPanel->state.panel_width; }
  int height() { return !hostPanel ? canvasHeight : hostPanel->internalRotation() & 1 ? hostPanel->state.panel_width : hostPanel->state.panel_height; }
  void record(const char* name, int x, int y, int w, int h, std::string text = "") {
    assert(x >= 0 && y >= 0 && w > 0 && h > 0 && x + w <= width() && y + h <= height());
    ops.push_back({name, text, x, y, w, h});
  }
  void fillScreen(uint16_t c) { fillRect(0, 0, width(), height(), c); }
  void drawPixel(int x, int y, uint16_t c) {
    if (x < 0 || y < 0 || x >= width() || y >= height()) return;
    int i = hostPanel ? hostPanel->address(x, y) : y*width()+x;
    if (i >= 0) pixels.at(i) = hostPanel ? c : (c >> 8) | (c << 8);
  }
  uint16_t readPixel(int x, int y) {
    if (x < 0 || y < 0 || x >= width() || y >= height()) return 0;
    int i = hostPanel ? hostPanel->address(x, y) : y*width()+x;
    return i < 0 ? 0 : pixels.at(i);
  }
  void startWrite() {} void endWrite() {}
  void writeCommand(uint8_t cmd) { hostPanel->writeCommand(cmd); }
  void writeData(uint8_t data) { hostPanel->writeData(data); }
  void writeData16(uint16_t data) { writeData(data >> 8); writeData(data & 255); }
  void pushImage(int x, int y, int w, int h, const uint16_t* p) {
    record("push", x, y, w, h);
    for (int row = 0; row < h; ++row) for (int col = 0; col < w; ++col) drawPixel(x + col, y + row, (p[row*w+col] >> 8) | (p[row*w+col] << 8));
  }
  void fillRect(int x, int y, int w, int h, uint16_t c) {
    record("fill", x, y, w, h);
    for (int row = y; row < y + h; ++row) for (int col = x; col < x + w; ++col) drawPixel(col, row, c);
  }
  void drawRect(int x, int y, int w, int h, uint16_t) { record("rect", x, y, w, h); }
  void fillRoundRect(int x, int y, int w, int h, int, uint16_t) { record("roundrect", x, y, w, h); }
  void fillCircle(int x, int y, int r, uint16_t) { record("filled-circle", x-r, y-r, r*2+1, r*2+1); }
  void fillTriangle(int x1, int y1, int x2, int y2, int x3, int y3, uint16_t) {
    int x = min({x1, x2, x3}), y = min({y1, y2, y3});
    record("triangle", x, y, max({x1, x2, x3})-x+1, max({y1, y2, y3})-y+1);
  }
  void drawFastVLine(int x, int y, int h, uint16_t) { record("line", x, y, 1, h); }
  void drawFastHLine(int x, int y, int w, uint16_t) { record("line", x, y, w, 1); }
  void drawCircle(int x, int y, int r, uint16_t) { record("circle", x-r, y-r, r*2+1, r*2+1); }
  void setTextDatum(int datum) { textDatum = datum; } void setTextSize(int n) { textSize = n; }
  template<class... T> void setTextColor(T...) {}
  void drawString(const char* s, int x, int y) {
    int w = strlen(s)*6*textSize, h = 8*textSize;
    if (textDatum == middle_center) { x -= w/2; y -= h/2; }
    record("text", x, y, w, h, s);
  }
  void drawNumber(int n, int x, int y) { drawString(std::to_string(n).c_str(), x, y); }
};
inline void gpio_lo(int) {} inline void gpio_hi(int) {}
namespace spi {
inline bool hostPressed = false;
inline void beginTransaction(int, int, int) {} inline void endTransaction(int) {}
inline void readBytes(int, uint8_t* data, size_t len) {
  // LovyanGFX 1.3.0 ESP32 common.inl copies the rounded TX length from data.
  // Volatile reads preserve that behavior under ASan, without real SPI access.
  assert(len == 33);
  volatile uint8_t tx = 0;
  for (size_t i = 0; i < ((len + 3) & ~size_t(3)); ++i) tx = data[i];
  (void)tx;
  assert(data[33] == 0 && data[34] == 0 && data[35] == 0);
  std::memset(data, 0, len);
  if (hostPressed) {
    for (int i = 0; i < 3; ++i) {
      uint8_t* d = data + (i+1)*8;
      const int adc[] = {1900, 1000, 1800, 3800}; // Y, Z1, X, Z2: pressure 1295
      for (int j = 0; j < 4; ++j) { d[j*2+1] = (adc[j] << 3) >> 8; d[j*2+2] = (adc[j] << 3) & 255; }
    }
  }
}
}
}
struct LGFX_Sprite : lgfx::LGFX_Device {
  lgfx::LGFX_Device* parent;
  explicit LGFX_Sprite(lgfx::LGFX_Device* p) : parent(p) {}
  void setColorDepth(int) {} void setPsram(bool) {}
  void* createSprite(int w, int h) { canvasWidth = w; canvasHeight = h; pixels.resize(w*h); return pixels.data(); }
  void* getBuffer() { return pixels.data(); }
  void pushSprite(int x, int y) { parent->pushImage(x, y, width(), height(), pixels.data()); }
};
