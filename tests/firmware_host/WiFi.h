#pragma once
#include "Arduino.h"
#include <ctime>
constexpr int WIFI_OFF = 0, WIFI_STA = 1, WL_CONNECTED = 3;
inline int hostRadioCalls = 0;
inline bool hostWifiConnected = false;
inline bool hostNtpAvailable = false;
inline bool getLocalTime(tm* t, uint32_t) { t->tm_hour = 12; return hostNtpAvailable; }
inline void configTzTime(const char*, const char*, const char*) {}
struct WiFiMock {
  void mode(int) { ++hostRadioCalls; delay(1000); }
  void setHostname(const char*) {} void setAutoReconnect(bool) {}
  void begin(const char*, const char*) {}
  int status() { return hostWifiConnected ? WL_CONNECTED : 0; }
  struct IP { String toString() { return "192.0.2.1"; } };
  IP localIP() { return {}; } int RSSI() { return -50; }
};
inline WiFiMock WiFi;
struct WiFiClient {
  explicit operator bool() const { return false; }
  bool connected() { return false; } void println(const char*) {} void stop() {} void setNoDelay(bool) {}
  int available() { return 0; } char read() { return 0; }
};
struct WiFiServer {
  explicit WiFiServer(int) {} void begin() {} void setNoDelay(bool) {} WiFiClient accept() { return {}; }
};
