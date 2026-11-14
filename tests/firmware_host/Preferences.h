#pragma once
#include "Arduino.h"
inline bool hostWifiConfigured = false;
struct Preferences {
  void begin(const char*, bool) {} void end() {} void clear() { hostWifiConfigured = false; }
  void putString(const char*, const String&) {}
  String getString(const char* key, const char* fallback) {
    if (!hostWifiConfigured) return fallback;
    if (!strcmp(key, "ssid")) return "deskbuddy-test-nonexistent";
    if (!strcmp(key, "tz")) return "UTC0";
    return "dummy-for-host-tests";
  }
};
