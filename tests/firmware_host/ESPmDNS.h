#pragma once
struct MDNSMock { bool begin(const char*) { return true; } void addService(const char*, const char*, int) {} void end() {} };
inline MDNSMock MDNS;
