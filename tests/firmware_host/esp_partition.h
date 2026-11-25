#pragma once
#include "Arduino.h"
struct esp_partition_t { size_t size; };
using esp_partition_subtype_t = int;
using esp_partition_mmap_handle_t = int;
using esp_err_t = int;
constexpr int ESP_PARTITION_TYPE_DATA = 1, ESP_PARTITION_MMAP_DATA = 1, ESP_OK = 0;
inline std::vector<uint8_t> hostArt;
inline esp_partition_t hostPart;
inline const char* esp_err_to_name(int) { return "host error"; }
inline const esp_partition_t* esp_partition_find_first(int, int, const char*) { hostPart.size = hostArt.size(); return &hostPart; }
inline int esp_partition_read(const esp_partition_t*, size_t off, void* out, size_t len) {
  if (off + len > hostArt.size()) return -1;
  memcpy(out, hostArt.data() + off, len); return ESP_OK;
}
inline int esp_partition_mmap(const esp_partition_t*, size_t, size_t, int, const void** ptr, int*) { *ptr = hostArt.data(); return ESP_OK; }
