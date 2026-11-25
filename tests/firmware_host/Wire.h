#pragma once
struct WireMock {
  void begin(int, int, int) {}
  void beginTransmission(int) {}
  void write(int) {}
  int endTransmission(bool = true) { return 1; }
  int requestFrom(int, int) { return 0; }
  int read() { return 0; }
};
inline WireMock Wire;
