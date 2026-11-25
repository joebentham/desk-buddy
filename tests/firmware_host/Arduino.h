#pragma once
#include <algorithm>
#include <cassert>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>
using std::min;
using std::max;
class String : public std::string {
 public:
  using std::string::string;
  String(const std::string& s) : std::string(s) {}
  bool startsWith(const String& prefix) const { return rfind(prefix, 0) == 0; }
  int lastIndexOf(char c) const { auto p = rfind(c); return p == npos ? -1 : int(p); }
  String substring(unsigned a, unsigned b = UINT32_MAX) const { return a >= size() ? "" : substr(a, b - a); }
  void trim() { auto a = find_first_not_of(" \t\r\n"); *this = a == npos ? "" : substr(a, find_last_not_of(" \t\r\n") - a + 1); }
  void toLowerCase() { for (char& c : *this) c = std::tolower(static_cast<unsigned char>(c)); }
  long toInt() const { return std::strtol(c_str(), nullptr, 10); }
  float toFloat() const { return std::strtof(c_str(), nullptr); }
};
inline uint32_t hostNow = 0;
inline uint32_t millis() { return hostNow; }
inline void delay(uint32_t ms) { hostNow += ms; }
inline uint32_t pdMS_TO_TICKS(uint32_t ms) { return ms; }
inline void vTaskDelay(uint32_t ms) { delay(ms); }
inline void xTaskCreatePinnedToCore(void (*)(void*), const char*, unsigned, void*, unsigned, void*, int) {}
inline void delayMicroseconds(int) {}
inline void pinMode(int, int) {}
inline void digitalWrite(int, int) {}
inline int digitalRead(int) { return 1; }
inline std::vector<int> hostBacklight;
inline void analogWrite(int, int value) { hostBacklight.push_back(value); }
inline long random(long n) { return n ? n / 2 : 0; }
inline void randomSeed(unsigned) {}
inline unsigned esp_random() { return 1; }
constexpr int OUTPUT = 1, INPUT = 0, HIGH = 1, LOW = 0;
struct SerialMock {
  std::string input, output;
  void begin(int) {}
  void flush() {}
  int available() { return input.size(); }
  char read() { char c = input.front(); input.erase(0, 1); return c; }
  void println(const char* s = "") { output += std::string(s) + "\n"; }
  template<class... T> void printf(const char* fmt, T... args) {
    char b[512]; std::snprintf(b, sizeof b, fmt, args...); output += b;
  }
};
inline SerialMock Serial;
struct ESPMock {
  unsigned getFreeHeap() { return 200000; }
  unsigned getMaxAllocHeap() { return 100000; }
  unsigned getPsramSize() { return 0; }
  void restart() { throw std::runtime_error("restart"); }
};
inline ESPMock ESP;
