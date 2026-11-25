// Desk Buddy — Lump frame player.
// Plays the hand-drawn frames baked from design/lump.html (tools/export_frames.py -> data/lump.bin).
// The frames live in their own flash partition ("lump"), not in the program, so the art and the
// program can each be updated over Wi-Fi on their own (tools/ota.py; see "Updates" below).
// The previous Vec firmware lives in archive/vec_main.cpp.
//
// Moods loop (idle, working, alert, done, waiting, longhaul, night). Changing mood always plays
// the hand-drawn transition; events (fidgets, oops, hello, tap) play once and return to their mood.
//
// Serial protocol (115200, newline-terminated), answered with "ok ..." or "err ...":
//   s <mood>          head to a mood: idle working alert done waiting longhaul night
//   s demo            showcase every mood and event on a loop
//   e <event>         play an event: fidget_toss fidget_twitch fidget_bubble oops hello tap
//                     ("tap" plays a random gag for the current mood: electric shock, ring of fire,
//                      wolf howl, accordion, anvil, disco, face on the glass, banana peel, balloon pop,
//                      sneeze, mooning, cartoon bomb; at night the late-night startle; during a meeting
//                      countdown or alarm, meeting prep)
//   p <0|1>           meeting countdown running (the bridge sends it): taps play meeting prep
//   o <working> <waiting> <unseen>   counts of sessions working, waiting on you,
//                     and finished-but-not-looked-at (ESP32-32E: bottom status band)
//   c <RRGGBB>        body colour (fades over ~1 s); default Tangerine FF7A45
//   i                 S3: IMU jolt; ESP32-32E: XPT2046 pressure, raw XY, taps, threshold
//   T <value>         S3: jolt in g (default .35); ESP32-32E: pressure 1..4095 (default 600)
//   P <invert 0|1> <bgr 0|1> <rotation 0..7>   ESP32-32E panel tuning until reset;
//                     P alone reports it. BGR is MADCTL bit 3, invert selects INVON.
//                     Default P 1 0 0: USB-C top, portrait 240x320, unmirrored.
//   h <secs>          heartbeat from the bridge, with the local time of day (seconds since midnight);
//                     no reply. Any command counts as hearing from the bridge.
//   ?                 report state
//   W                 Wi-Fi status: connected or not, IP, signal, where the clock comes from
//   w <ssid>\t<password>\t<token>\t<tz>   save Wi-Fi details (USB only; tools/wifi_setup.py
//                     sends this so the password never passes through the bridge or its log),
//                     then restart. "w" alone forgets them.
// The board prints "tap" when the glass is tapped (and giggles itself when that makes sense).
// It prints "gag tap_<gag>_<mood>" the moment a gag starts playing (the bridge plays its sound in step).
//
// On his own: with no word from the bridge for a minute (or since boot) he's "offline": he
// drops what he was showing, idles and fidgets (asleep at night if a heartbeat ever told him the
// time), and shows a small unplugged-cable mark in the bottom-right corner. Just powered from a
// charger, he lives like this. When the bridge speaks again he prints "resync" so it resends.
//
// Wi-Fi (once set up): he joins your network as lump.local and takes the same commands over TCP
// port 4210, so the bridge can drive him without a USB data cable. A connection must start with
// "k <token>" (the pairing token from setup) or it's dropped. Replies go to USB and to the
// network client. He also sets his clock from the internet, so he sleeps at night on his own.
//
// Updates over Wi-Fi (tools/ota.py does all this): a small web server on port 80, every request
// carrying the pairing token in X-Lump-Token.
//   GET  /info?app_len=N&art_len=M   SHA-256 of the first N bytes of the running program and the
//                                    first M bytes of the art, so the uploader sends only what changed
//   POST /update/app                 new firmware.bin (application/octet-stream, X-Lump-MD5 checked),
//                                    written to the other program slot, then restart into it
//   POST /update/art                 new lump.bin (X-Lump-Sha256 checked) into the art partition, then restart
// While an update streams in he shows a progress bar instead of animating. If the art is missing or
// bad he says so on screen but keeps Wi-Fi and updates working, so it can be fixed without a cable.
// On its own the device also: fidgets every minute or so while asleep, escalates "alert" to
// "waiting" after 45 s, slides "working" into "longhaul" after 10 min, and dims at night.

#include <Arduino.h>
#include <LovyanGFX.hpp>
#include <Wire.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <time.h>
#include <stdarg.h>
#ifndef LUMP_NO_OTA
#include <WebServer.h>
#include <Update.h>
#endif
#include <esp_partition.h>
#include <esp_ota_ops.h>
#include <spi_flash_mmap.h>
#include "mbedtls/sha256.h"

#ifdef BOARD_ESP32_32E
#include "board_esp32_32e.h"
constexpr const char* ART_RECOVERY_COMMAND = "tools/flash.sh esp32-32e";
#else
constexpr int PIN_SCLK = 40, PIN_MOSI = 41, PIN_DC = 38, PIN_CS = 39, PIN_RST = 42, PIN_BL = 20;
constexpr bool PRISM_MIRROR = true;  // rotations 4–7 are mirrored for the prism
constexpr const char* ART_RECOVERY_COMMAND = "tools/ota.py --art";
#endif
constexpr uint32_t ESCALATE_MS = 45000, LONGHAUL_MS = 10UL * 60 * 1000;
constexpr uint8_t BL_DAY = 255, BL_NIGHT = 70;
constexpr uint32_t HOST_TIMEOUT_MS = 60000;    // bridge heartbeats every 15 s
constexpr uint32_t OFFLINE_MARK_AFTER_MS = 8000;  // at boot, give the bridge a moment before showing the mark
constexpr uint8_t NIGHT_FROM = 0, NIGHT_UNTIL = 8;   // midnight to 8am; matches the bridge

#ifndef BOARD_ESP32_32E
class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_ST7789 panel;
  lgfx::Bus_SPI bus;
 public:
  LGFX() {
    auto b = bus.config();
    b.spi_host = SPI2_HOST; b.spi_mode = 0; b.freq_write = 80000000;
    b.pin_sclk = PIN_SCLK; b.pin_mosi = PIN_MOSI; b.pin_miso = -1; b.pin_dc = PIN_DC;
    b.dma_channel = SPI_DMA_CH_AUTO;
    bus.config(b); panel.setBus(&bus);
    auto p = panel.config();
    p.pin_cs = PIN_CS; p.pin_rst = PIN_RST; p.pin_busy = -1;
    p.panel_width = 240; p.panel_height = 240; p.memory_width = 240; p.memory_height = 320;
    p.invert = true; p.readable = false;
    panel.config(p); setPanel(&panel);
  }
};
#endif
static LGFX tft;
static LGFX_Sprite fb(&tft);
#ifdef BOARD_ESP32_32E
static LGFX_Sprite bandFb(&tft); // 38,400 bytes; composed before one panel push
#endif

// ---------- animation data: the "lump" flash partition, memory-mapped ----------
static const esp_partition_t* artPart = nullptr;
static const uint8_t* art = nullptr;            // mapped start of the partition
static size_t artSize = 0;                      // mapped length (the whole partition)
static esp_partition_mmap_handle_t artMap;
static bool mapArt() {
  artPart = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, (esp_partition_subtype_t)0x40, "lump");
  if (!artPart) return false;
  size_t length = artPart->size;
#ifdef BOARD_ESP32_32E
  // Classic ESP32 has a 4 MB DROM window (64 x 64 KB pages), shared with program
  // rodata. v4 records its exact length: map only live art, not partition padding.
  uint8_t header[18];
  if (esp_partition_read(artPart, 0, header, sizeof header) != ESP_OK ||
      memcmp(header, "LUMP\x04\x00", 6) != 0) return false;
  length = uint32_t(header[14]) | uint32_t(header[15]) << 8 | uint32_t(header[16]) << 16 | uint32_t(header[17]) << 24;
  if (length < sizeof header || length > artPart->size || length > 0x270000) return false;
#endif
  const void* ptr = nullptr;
  Serial.printf("DROM pages free before art=%u, art needs=%u (64 KB pages)\n",
                unsigned(spi_flash_mmap_get_free_pages(SPI_FLASH_MMAP_DATA)), unsigned((length + 65535) / 65536));
  esp_err_t err = esp_partition_mmap(artPart, 0, length, ESP_PARTITION_MMAP_DATA, &ptr, &artMap);
  if (err != ESP_OK) { Serial.printf("err art mmap bytes=%u: %s\n", unsigned(length), esp_err_to_name(err)); return false; }
  art = (const uint8_t*)ptr; artSize = length;
  Serial.printf("art mapped: %u bytes (partition %u)\n", unsigned(artSize), unsigned(artPart->size));
  return true;
}

constexpr int MAX_MOODS = 12, MAX_SEQ = 250;
enum Kind : uint8_t { LOOP = 0, TRANS = 1, EVENT = 2 };
struct Anim {
  uint16_t nFrames = 0; uint8_t fps = 24, nMoods = 0, nSeqs = 0;
  uint16_t width = 240, height = 240;
  uint8_t base[MAX_MOODS]; String moodName[MAX_MOODS];
  uint8_t kind[MAX_SEQ], from[MAX_SEQ], to[MAX_SEQ]; uint16_t len[MAX_SEQ]; const uint8_t* seq[MAX_SEQ]; String name[MAX_SEQ];
  int8_t loopOf[MAX_MOODS], transOf[MAX_MOODS][MAX_MOODS];
  const uint8_t* offsets; const uint8_t* data;
  bool ok = false;
} A;

static inline uint16_t rd16(const uint8_t* p) { return p[0] | (p[1] << 8); }
static inline uint32_t rd32(const uint8_t* p) { return p[0] | (p[1] << 8) | (p[2] << 16) | (uint32_t(p[3]) << 24); }
static String rdStr(const uint8_t*& p) { int n = *p++; String s; s.reserve(n); for (int i = 0; i < n; i++) s += char(p[i]); p += n; return s; }
static bool validMood(int m) { return m >= 0 && m < A.nMoods; }
static int moodIndex(const String& n) { for (int m = 0; m < A.nMoods; m++) if (A.moodName[m] == n) return m; return -1; }

// Every read is bounds-checked against the partition: half-written or garbage art must fail
// cleanly (and leave Wi-Fi updates working), never crash.
static bool parseAnim() {
  A.ok = false;
  A.width = A.height = 240;
  if (!art || artSize < 16) return false;
  const uint8_t* p = art; const uint8_t* end = art + artSize;
  auto room = [&](size_t n) { return size_t(end - p) >= n; };
  if (memcmp(p, "LUMP", 4) != 0) return false;
  p += 4;
  uint16_t version = rd16(p); p += 2;
  if (version != 3 && version != 4) return false;
  A.nFrames = rd16(p); p += 2;
  A.nMoods = *p++; A.fps = *p++;
  if (!A.nFrames || !A.fps || !A.nMoods || A.nMoods > MAX_MOODS) return false;
  if (version == 4) {
    if (!room(8)) return false;
    A.width = rd16(p); A.height = rd16(p + 2); uint32_t length = rd32(p + 4); p += 8;
    if (length < size_t(p - art) || length > artSize) return false;
    artSize = length; end = art + length;
  }
#ifdef BOARD_ESP32_32E
  if (A.width != 120 || A.height != 120) return false;
#else
  if (A.width != 240 || A.height != 240) return false;
#endif
  for (int m = 0; m < A.nMoods; m++) {
    if (!room(2) || !room(2 + p[1])) return false;
    A.base[m] = *p++; A.moodName[m] = rdStr(p); A.loopOf[m] = -1;
    if (A.base[m] >= A.nMoods || !A.moodName[m].length()) return false;
    for (int prev = 0; prev < m; ++prev) if (A.moodName[prev] == A.moodName[m]) return false;
  }
  // Demo, offline mode and automatic escalation look these up by name. A file
  // can have valid lengths, indices and RLE while a damaged name is missing.
  for (const char* required : {"idle", "working", "alert", "done", "waiting", "longhaul", "night"})
    if (moodIndex(required) < 0) return false;
  memset(A.transOf, -1, sizeof(A.transOf));
  if (!room(1)) return false;
  A.nSeqs = *p++;
  if (A.nSeqs > MAX_SEQ) return false;
  for (int i = 0; i < A.nSeqs; i++) {
    if (!room(4) || !room(4 + p[3] + 2)) return false;
    A.kind[i] = p[0]; A.from[i] = p[1]; A.to[i] = p[2]; p += 3;
    if (A.kind[i] > EVENT) return false;
    if (A.from[i] >= A.nMoods || A.to[i] >= A.nMoods) return false;
    A.name[i] = rdStr(p);
    A.len[i] = rd16(p); p += 2;
    if (!A.len[i] || !room(size_t(A.len[i]) * 2)) return false;
    A.seq[i] = p; p += A.len[i] * 2;
    for (int k = 0; k < A.len[i]; k++) if (rd16(A.seq[i] + 2 * k) >= A.nFrames) return false;
    if (A.kind[i] == LOOP) A.loopOf[A.from[i]] = i;
    else if (A.kind[i] == TRANS) A.transOf[A.from[i]][A.to[i]] = i;
  }
  for (int m = 0; m < A.nMoods; m++) if (A.loopOf[m] < 0) return false;
  if (!room(size_t(A.nFrames) * 4)) return false;
  A.offsets = p; p += A.nFrames * 4;
  A.data = p;
  // Check every run once at boot. Decoding may then use direct flash pointers,
  // including on a board with no PSRAM, without overreads or zero-run hangs.
  for (int f = 0; f < A.nFrames; f++) {
    uint32_t off = rd32(A.offsets + 4 * f);
    uint32_t stop = f + 1 < A.nFrames ? rd32(A.offsets + 4 * (f + 1)) : end - A.data;
    if (off >= stop || stop > size_t(end - A.data)) return false;
    uint32_t pixels = 0;
    for (; off < stop && pixels < uint32_t(A.width) * A.height; off += 3) {
      if (stop - off < 3 || !A.data[off]) return false;
      pixels += A.data[off];
      if (pixels > uint32_t(A.width) * A.height) return false;
    }
    if (pixels != uint32_t(A.width) * A.height) return false;
    // v3 has no file length: its final frame is followed by erased partition
    // padding. v4 records its exact end; all earlier frames end at next offset.
    if ((version == 4 || f + 1 < A.nFrames) && off != stop) return false;
    if (!(f & 31)) delay(1);
  }
  return A.ok = true;
}
static int eventIndex(const String& n) { for (int i = 0; i < A.nSeqs; i++) if (A.kind[i] == EVENT && A.name[i] == n) return i; return -1; }

// ---------- body colour ----------
// Frames are exported with his body in pure magenta (FF00FF), the only colour besides the cream
// line (FFF1DC) and black (props with their own colour, like the yellow banana, are left alone). Every pixel is some mix  alpha*magenta + beta*cream  (plus black), and
// since magenta has no green, beta = G/241 and alpha = R/255 - beta. Swapping magenta for the
// current body colour recolours the fill, its soft edges, the tongue and the blanket alike.
static float tintR = 255, tintG = 122, tintB = 69;        // current (fading) colour, Tangerine
static float goalR = 255, goalG = 122, goalB = 69;        // colour we're fading to
static inline uint16_t sw16(uint16_t v) { return (v >> 8) | (v << 8); }
static uint16_t recolor(uint16_t stored) {               // stored = byte-swapped RGB565
  uint16_t v = sw16(stored);
  int r = (v >> 11) << 3, g = ((v >> 5) & 63) << 2, b = (v & 31) << 3;
  if (r < 8 && g < 8 && b < 8) return stored;            // black stays black
  float beta = g / 241.f, alpha = r / 255.f - beta;
  if (alpha <= 0.02f) return stored;                     // pure line colour: unchanged
  if (b < alpha * 255 + beta * 220 - 48) return stored;  // a real colour (the yellow banana): keep it
  int nr = min(255, int(alpha * tintR + beta * 255)), ng = min(255, int(alpha * tintG + beta * 241)), nb = min(255, int(alpha * tintB + beta * 220));
  return sw16(lgfx::color565(nr, ng, nb));
}

// RLE runs of [count][rgb565 big-endian]. The sprite buffer wants byte-swapped RGB565,
// which is big-endian byte order, so each run's two bytes go in as stored (then recoloured).
static void decodeFrame(uint16_t idx) {
  const uint8_t* p = A.data + rd32(A.offsets + 4 * idx);
  uint16_t* out = (uint16_t*)fb.getBuffer();
  int i = 0;
  uint16_t lastIn = 0xFFFF, lastOut = 0;
  int pixels = A.width * A.height;
  while (i < pixels) {
    int n = p[0]; uint16_t v = p[1] | (p[2] << 8); p += 3;
    if (v != lastIn) { lastIn = v; lastOut = recolor(v); }
    v = lastOut;
    if (i + n > pixels) n = pixels - i;
    for (int k = 0; k < n; k++) out[i + k] = v;
    i += n;
  }
}

// ---------- player ----------
// Always inside one sequence: a mood's loop, a transition, or an event. Nothing ever cuts:
// a new target waits for the current transition/event to finish, then routes there.
static int target = 0, curMood = 0, curSeq = 0, pendingEvent = -1;
static uint32_t seqStart = 0, loopSince = 0, nextFidget = 0;
static int lastFrame = -1;
static float fps = 0;

static void say(const char* fmt, ...);   // below, with the link code
static void startSeq(int s) { curSeq = s; seqStart = millis(); lastFrame = -1; }
static int nextHop(int a, int b) {
  if (!A.ok || !validMood(a) || !validMood(b)) return -1;
  if (A.transOf[a][b] >= 0) return A.transOf[a][b];
  if (A.base[a] != a) return A.transOf[a][A.base[a]];
  if (A.base[b] != b && A.transOf[a][A.base[b]] >= 0) return A.transOf[a][A.base[b]];
  return -1;
}
static void settle(int m) {  // arrived in mood m's loop
  if (!A.ok || !validMood(m) || !validMood(target)) return;
  curMood = m;
  if (target != m) {
    int h = nextHop(m, target);
    if (h >= 0) return startSeq(h);
    curMood = m = target;  // nothing drawn for this route: cut (shouldn't happen)
  }
  // a gag queued for a mood he never settled in (the bridge moved him on meanwhile): play the same
  // gag in the mood he did land in, rather than silently dropping it
  if (pendingEvent >= 0 && A.from[pendingEvent] != m) {
    String n = A.name[pendingEvent]; int u = n.lastIndexOf('_'), e2 = -1;
    if (u > 0) { e2 = eventIndex(n.substring(0, u) + "_" + A.moodName[m]); if (e2 < 0) e2 = eventIndex(n.substring(0, u) + "_" + A.moodName[A.base[m]]); }
    pendingEvent = e2;
  }
  if (pendingEvent >= 0 && A.from[pendingEvent] == m) {
    int e = pendingEvent; pendingEvent = -1;
    // tell the bridge the moment a gag actually starts (it may have waited for a transition), so it can play its sound in step
    if (A.name[e].startsWith("tap_")) say("gag %s", A.name[e].c_str());
    return startSeq(e);
  }
  startSeq(A.loopOf[m]); loopSince = millis();
}
static bool setTarget(int m) {
  if (!A.ok || !validMood(m)) return false;
  target = m;
  if (A.kind[curSeq] == LOOP) settle(curMood);
  return true;
}
// returns false if there's no such event
// Tapping the glass: in the daytime every tap gets one of his gags at random, never the same one twice
// running. Every daytime mood has its own version of each (tap_<gag>_<mood>). Two taps depend on the moment:
// at night he only does the soft late-night startle (tap_startle_night), and while a meeting is coming up
// (the bridge says "p 1") or the meeting alarm is ringing he only does meeting prep (tap_prep_<mood>).
static bool prepMode = false;      // the bridge's meeting countdown is running
static const char* pickTap() {
  static const char* GAG[] = {"shock", "moto", "wolf", "accordion", "anvil", "disco", "glass", "banana", "balloon", "sneeze", "moon", "bomb"};
  static const int W[] = {2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2};
  constexpr int NG = sizeof(W) / sizeof(W[0]);
  static int last = -1;
  int total = 0; for (int i = 0; i < NG; i++) if (i != last) total += W[i];
  int x = random(total);
  for (int i = 0; i < NG; i++) { if (i == last) continue; if ((x -= W[i]) < 0) { last = i; return GAG[i]; } }
  return GAG[0];
}
static bool playEvent(String n) {
  if (!A.ok || !validMood(target)) return false;
  if (n == "tap") {
    const String& m = A.moodName[target];
    n = m == "night" ? String("tap_startle") : (prepMode || m == "meeting") ? String("tap_prep") : String("tap_") + pickTap();
  }
  int e = eventIndex(n);
  if (e < 0) {  // generic names like "tap" resolve per mood: tap_idle, tap_working, ...
    e = eventIndex(n + "_" + A.moodName[target]);
    if (e < 0) e = eventIndex(n + "_" + A.moodName[A.base[target]]);
  }
  if (e < 0) return false;
  pendingEvent = e;
  if (target != A.from[e]) setTarget(A.from[e]); else if (A.kind[curSeq] == LOOP) settle(curMood);
  return true;
}

// ---------- demo: a showcase of every mood and event ----------
struct Step { char type; const char* name; uint32_t ms; };
static const Step DEMO[] = {
  {'s', "idle", 6000}, {'e', "fidget_bubble", 6500}, {'e', "fidget_twitch", 3500},
  {'s', "working", 6000}, {'e', "oops", 7000}, {'e', "tap", 5000}, {'s', "longhaul", 6500},
  {'s', "alert", 6000}, {'s', "waiting", 9000}, {'s', "done", 5000}, {'e', "tap", 4500},
  {'s', "idle", 4000}, {'e', "fidget_toss", 5500}, {'e', "hello", 7000},
  {'s', "night", 9000}, {'s', "idle", 3000},
};
static bool demo = false;
static int demoStep = -1;
static uint32_t demoNext = 0;
static void runDemo(uint32_t now) {
  if (!A.ok || !demo || now < demoNext) return;
  demoStep = (demoStep + 1) % (sizeof(DEMO) / sizeof(DEMO[0]));
  const Step& s = DEMO[demoStep];
  if (s.type == 's') {
    if (!setTarget(moodIndex(s.name))) { demo = false; say("err demo mood missing"); return; }
  } else playEvent(s.name);
  demoNext = now + s.ms;
}

// ---------- things he does on his own ----------
static void autoBehaviour(uint32_t now) {
  if (!A.ok || demo || A.kind[curSeq] != LOOP || target != curMood) return;
  const String& m = A.moodName[curMood];
  if (m == "idle" && pendingEvent < 0) {
    if (!nextFidget) nextFidget = now + 40000 + random(50000);
    if (now > nextFidget) {
      static const char* F[] = {"fidget_toss", "fidget_twitch", "fidget_bubble"};
      nextFidget = 0; playEvent(F[random(3)]);
    }
  } else nextFidget = 0;
  if (m == "alert" && now - loopSince > ESCALATE_MS && !setTarget(moodIndex("waiting"))) say("err waiting mood missing");
  if (m == "working" && now - loopSince > LONGHAUL_MS && !setTarget(moodIndex("longhaul"))) say("err longhaul mood missing");
}

// ---------- backlight: dims gently at night ----------
static void backlight(uint32_t now) {
  static uint32_t last = 0; static float level = BL_DAY;
  if (now - last < 20) return;
  last = now;
  bool night = A.kind[curSeq] == LOOP && A.moodName[curMood] == "night";
  level += ((night ? BL_NIGHT : BL_DAY) - level) * 0.04f;
  analogWrite(PIN_BL, int(level));
}

static volatile bool tapPending = false;
static volatile uint32_t tapCount = 0;
#ifndef BOARD_ESP32_32E
// ---------- motion sensor (QMI8658 on I2C SDA 47 / SCL 48): tapping the glass ----------
// Sampled ~400x a second on core 0 so short taps aren't missed while core 1 is drawing.
// A tap is a sharp jolt: the acceleration jumps away from its slow-moving average.
constexpr int IMU_SDA = 47, IMU_SCL = 48;
static uint8_t imuAddr = 0;
static volatile float tapThreshold = 0.35f, peakJolt = 0;
static bool imuWrite(uint8_t reg, uint8_t v) { Wire.beginTransmission(imuAddr); Wire.write(reg); Wire.write(v); return Wire.endTransmission() == 0; }
static bool imuRead(uint8_t reg, uint8_t* buf, int n) {
  Wire.beginTransmission(imuAddr); Wire.write(reg); if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)imuAddr, n) != n) return false;
  for (int i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}
static bool imuInit() {
  Wire.begin(IMU_SDA, IMU_SCL, 400000);
  for (uint8_t a : {0x6B, 0x6A}) {
    imuAddr = a; uint8_t who = 0;
    if (imuRead(0x00, &who, 1) && who == 0x05) {
      imuWrite(0x60, 0xB0); delay(20);            // soft reset
      imuWrite(0x02, 0x40);                       // CTRL1: register auto-increment, little endian
      imuWrite(0x03, 0x15);                       // CTRL2: accel +-4 g, 250 Hz
      imuWrite(0x08, 0x01);                       // CTRL7: accelerometer on
      return true;
    }
  }
  imuAddr = 0; return false;
}
static void imuTask(void*) {
  float avg = 1.f; uint32_t lastTap = 0, peakAt = 0;
  for (;;) {
    uint8_t b[6];
    if (imuRead(0x35, b, 6)) {
      float x = int16_t(b[0] | b[1] << 8) / 8192.f, y = int16_t(b[2] | b[3] << 8) / 8192.f, z = int16_t(b[4] | b[5] << 8) / 8192.f;
      float mag = sqrtf(x * x + y * y + z * z), jolt = fabsf(mag - avg);
      avg += (mag - avg) * 0.02f;
      uint32_t now = millis();
      if (jolt > peakJolt || now - peakAt > 4000) { peakJolt = jolt; peakAt = now; }
      if (jolt > tapThreshold && now - lastTap > 600) { lastTap = now; tapCount++; tapPending = true; }
    }
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}
#else
// Shared SPI bus: touch polling and drawing both run in loop(), with separate
// transactions/CS lines. No competing core/task can corrupt a panel transfer.
static uint16_t tapThreshold = 600, touchPressure = 0, touchPeak = 0;
static int32_t touchX = 0, touchY = 0;
static void pollTouch(uint32_t now) {
  static uint32_t lastSample = 0, pressAt = 0, releaseAt = 0, lastTap = 0;
  static bool latched = false;
  if (now - lastSample < 20) return;
  lastSample = now;
  touchPressure = tft.rawTouch(&touchX, &touchY);
  touchPeak = max(touchPeak, touchPressure);
  if (touchPressure >= tapThreshold) {
    releaseAt = 0;
    if (!pressAt) pressAt = now;
    if (!latched && now - pressAt >= 40 && now - lastTap >= 600) {
      latched = true; lastTap = now; tapCount = tapCount + 1; tapPending = true;
    }
  } else {
    pressAt = 0;
    if (!releaseAt) releaseAt = now;
    if (now - releaseAt >= 80) latched = false;
  }
}
#endif

// ---------- talking back: USB, and the Wi-Fi client if there is one ----------
constexpr uint16_t NET_PORT = 4210;
static String wifiSsid, netToken, tzRule;     // from flash (Preferences "lump"); password stays there
static bool wifiStarted = false, mdnsUp = false, netAuthed = false;
static WiFiServer server(NET_PORT);
static WiFiClient net;
static void say(const char* fmt, ...) {
  char b[256]; va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof b, fmt, ap); va_end(ap);
  Serial.println(b);
  if (netAuthed && net.connected()) net.println(b);
}

// ---------- on his own: no bridge ----------
static uint32_t lastHost = 0, clockAt = 0;
static int32_t clockBase = -1;                 // seconds since local midnight at clockAt; -1 = unknown
static bool hostSeen = false, offline = true;  // offline until the bridge says something
static int ovWorking = 0, ovWaiting = 0, ovUnseen = 0;
static bool hostAlive(uint32_t now) { return hostSeen && now - lastHost < HOST_TIMEOUT_MS; }
static bool nightHour(int h) {                 // works whether or not the window wraps past midnight
  return NIGHT_FROM < NIGHT_UNTIL ? (h >= NIGHT_FROM && h < NIGHT_UNTIL) : (h >= NIGHT_FROM || h < NIGHT_UNTIL);
}
static bool nightNow(uint32_t now) {
  struct tm t;                                 // the internet's clock first, then the bridge's
  if (tzRule.length() && getLocalTime(&t, 0)) return nightHour(t.tm_hour);
  if (clockBase < 0) return false;
  return nightHour(((clockBase + (now - clockAt) / 1000) % 86400) / 3600);
}
static void heardFromHost() {
  bool was = offline;
  lastHost = millis(); hostSeen = true; offline = false;
  if (was) say("resync");           // the bridge resends mood, colour and corner extras
}
static void runOffline(uint32_t now) {
  if (!offline && !hostAlive(now)) {           // lost the bridge: return to autonomous behaviour
    offline = true;
#ifndef BOARD_ESP32_32E
    ovWorking = ovWaiting = ovUnseen = 0;
#endif
    // The 32E band retains the last counts as a snapshot, marked OFF. This
    // also keeps a USB counter demonstration visible after the port closes.
    prepMode = false;
    goalR = 255; goalG = 122; goalB = 69;      // back to Tangerine (a usage or meeting colour would be stale)
  }
  if (!offline || demo) return;
  int want = moodIndex(nightNow(now) ? "night" : "idle");
  if (want >= 0 && target != want && pendingEvent < 0 && A.kind[curSeq] == LOOP) setTarget(want);
}
static bool offlineMark(uint32_t now) { return offline && !demo && now > OFFLINE_MARK_AFTER_MS; }
// a little unplugged cable, gently reaching for its socket
#ifndef BOARD_ESP32_32E
static void drawOfflineMark(uint32_t now) {
  float t = now / 1000.f, k = 0.45f + 0.3f * sinf(t * 2.1f);
  uint16_t c = lgfx::color565(255 * k, 241 * k, 220 * k);
  int px = 200 + int(2.5f + 2.5f * sinf(t * 1.3f));  // plug drifts 0..5 px, never reaches the socket
  fb.fillRect(186, 226, px - 186, 2, c);              // cable
  fb.fillRoundRect(px, 221, 9, 12, 2, c);             // plug
  fb.fillRect(px + 9, 223, 4, 2, c); fb.fillRect(px + 9, 229, 4, 2, c);  // prongs
  fb.fillRoundRect(220, 219, 4, 16, 1, c);            // socket
}
#endif

// ---------- corner extras ----------
// Small marks drawn over the animation so nothing is hidden when several sessions are busy:
//   bottom-left  three bouncing dots: something is working in the background
//   top-left     amber dots, one per session waiting on you (when more than he can show)
//   top-right    a twinkling sparkle: something finished that you haven't looked at yet
static uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) { return lgfx::color565(r, g, b); }
#ifndef BOARD_ESP32_32E
static bool overlayVisible() {
  if (offlineMark(millis())) return true;
  const String& m = A.moodName[target];
  bool workingMood = m == "working" || m == "longhaul", alertMood = m == "alert" || m == "waiting";
  return (ovWorking > 0 && !workingMood) || ovWaiting >= 2 || (ovWaiting >= 1 && !alertMood) || (ovUnseen > 0 && m != "done");
}
static void drawOverlay(uint32_t now) {
  if (offlineMark(now)) drawOfflineMark(now);
  const String& m = A.moodName[target];
  bool workingMood = m == "working" || m == "longhaul", alertMood = m == "alert" || m == "waiting";
  float t = now / 1000.f;
  if (ovWorking > 0 && !workingMood) {
    for (int i = 0; i < 3; i++) {
      float b = max(0.f, sinf(t * 6 - i * 0.9f));
      fb.fillCircle(16 + i * 10, 226 - b * 4, 2.5f, rgb(255 * (0.45f + 0.55f * b), 241 * (0.45f + 0.55f * b), 220 * (0.45f + 0.55f * b)));
    }
  }
  if (ovWaiting >= 2 || (ovWaiting >= 1 && !alertMood)) {
    float pulse = 0.6f + 0.4f * sinf(t * 5);
    for (int i = 0; i < min(ovWaiting, 5); i++) fb.fillCircle(14 + i * 11, 14, 3.5f, rgb(255 * pulse, 181 * pulse, 71 * pulse));
  }
  if (ovUnseen > 0 && m != "done") {
    for (int k = 0; k < min(ovUnseen, 2); k++) {
      float s = (k ? 4.f : 6.f) * (0.75f + 0.25f * sinf(t * 4 + k * 2));
      int x = 224 - k * 13, y = 16 + k * 6;
      uint16_t c = rgb(200, 255, 210);
      fb.fillTriangle(x, y - s, x + s * 0.3f, y, x - s * 0.3f, y, c); fb.fillTriangle(x, y + s, x + s * 0.3f, y, x - s * 0.3f, y, c);
      fb.fillTriangle(x - s, y, x, y - s * 0.3f, x, y + s * 0.3f, c); fb.fillTriangle(x + s, y, x, y - s * 0.3f, x, y + s * 0.3f, c);
    }
  }
}
#endif

#ifdef BOARD_ESP32_32E
// Native text below the art, composed off-screen without erasing live text.
// Odd tuning rotations get a right-hand band so live experiments remain safe.
struct BandLayout {
  int x, y, w, h;
  int countX[3], countY[3];
  int offlineX, offlineY, tapX, tapY;
};
static BandLayout bandLayout(int width, int height) {
  if (width < height) return {0, 240, width, height - 240,
    {12, 92, 172}, {252, 252, 252}, 12, 300, 216, 300};
  return {240, 0, width - 240, height,
    {250, 250, 250}, {12, 68, 124}, 248, 176, 280, 216};
}
static PanelSettings panelSettings = BOARD_PANEL_DEFAULTS;
constexpr uint32_t TOUCH_FEEDBACK_MS = 450;
static uint32_t touchFeedbackAt = 0;
static bool touchFeedback = false;
struct BandContent {
  int counts[3];
  bool offline, tap;
  bool operator==(const BandContent& b) const {
    return counts[0] == b.counts[0] && counts[1] == b.counts[1] && counts[2] == b.counts[2]
           && offline == b.offline && tap == b.tap;
  }
};
static void drawBand(uint32_t now, bool force = false) {
  static bool valid = false, allocationFailed = false;
  static BandContent previous{};
  if (touchFeedback && now - touchFeedbackAt >= TOUCH_FEEDBACK_MS) touchFeedback = false;
  BandContent content = {{min(ovWorking, 1000), min(ovWaiting, 1000), min(ovUnseen, 1000)},
                         offlineMark(now), touchFeedback};
  BandLayout b = bandLayout(tft.width(), tft.height());
  bool resized = bandFb.width() != b.w || bandFb.height() != b.h;
  if (resized) {
    if (allocationFailed && !force) return;
    bandFb.setColorDepth(16); bandFb.setPsram(false);
    allocationFailed = bandFb.createSprite(b.w, b.h) == nullptr;
    if (allocationFailed) { Serial.println("err status framebuffer allocation failed"); return; }
  }
  if (!force && !resized && valid && content == previous) return;
  bandFb.fillScreen(TFT_BLACK);
  uint16_t rule = rgb(45, 42, 38), cream = rgb(255, 241, 220);
  if (b.y) bandFb.drawFastHLine(0, 0, b.w, rule);
  else bandFb.drawFastVLine(0, 0, b.h, rule);
  bandFb.setTextDatum(top_left); bandFb.setTextSize(1);
  const char* labels[] = {"WORK", "WAIT", "DONE"};
  uint16_t colours[] = {cream, rgb(255, 181, 71), rgb(200, 255, 210)};
  for (int i = 0; i < 3; ++i) {
    int x = b.countX[i] - b.x, y = b.countY[i] - b.y;
    bandFb.setTextColor(colours[i]); bandFb.setTextSize(1);
    bandFb.drawString(labels[i], x, y);
    bandFb.setTextSize(2);
    if (content.counts[i] > 999) bandFb.drawString("999+", x, y + 14);
    else bandFb.drawNumber(content.counts[i], x, y + 14);
  }
  bandFb.setTextSize(1); bandFb.setTextColor(cream);
  if (content.offline) {
    int x = b.offlineX - b.x, y = b.offlineY - b.y;
    bandFb.drawString("OFF", x, y - 3);
    bandFb.drawRect(x + 30, y - 6, 9, 12, cream);
    bandFb.drawFastHLine(x + 24, y - 1, 6, cream);
    bandFb.drawFastHLine(x + 39, y - 4, 4, cream);
    bandFb.drawFastHLine(x + 39, y + 3, 4, cream);
  }
  if (content.tap) {
    // Raw touch XY is intentionally uncalibrated. A fixed TAP ring acknowledges
    // every accepted press immediately, even while art is between gag frames.
    int x = b.tapX - b.x, y = b.tapY - b.y;
    bandFb.drawString("TAP", x - (b.y ? 44 : 8), y - (b.y ? 3 : 30));
    bandFb.drawCircle(x, y, 8, cream);
    bandFb.drawCircle(x, y, 12, rgb(255, 122, 69));
  }
  bandFb.pushSprite(b.x, b.y);
  previous = content; valid = true;
}
static void pushFrame() {
  // Explicit integer pixel duplication: no interpolation of the magenta marker.
  const uint16_t* src = static_cast<const uint16_t*>(fb.getBuffer());
  static uint16_t rows[240 * 2];
  tft.startWrite();
  for (int y = 0; y < 120; ++y) {
    for (int x = 0; x < 120; ++x) rows[2*x] = rows[2*x+1] = src[y*120+x];
    memcpy(rows + 240, rows, 240 * sizeof(uint16_t));
    tft.pushImage(0, y * 2, 240, 2, rows);
  }
  tft.endWrite();
}
#endif

static void drawRecoveryMessage() {
  tft.setTextColor(lgfx::color565(255, 241, 220)); tft.setTextDatum(middle_center);
  tft.setTextSize(1);
  tft.drawString("no drawings yet", 120, 104); tft.drawString(ART_RECOVERY_COMMAND, 120, 124);
}

// ---------- serial ----------
static void reply() {
  if (!A.ok) { say("err lump art missing or bad"); return; }
  say("ok mode=%s target=%s playing=%s(%s) fps=%.1f frames=%u seqs=%u",
    demo ? "demo" : offline ? "offline" : "direct", A.moodName[target].c_str(), A.name[curSeq].c_str(),
    A.kind[curSeq] == LOOP ? "loop" : A.kind[curSeq] == TRANS ? "transition" : "event", fps, A.nFrames, A.nSeqs);
}
static void setWifi(String a);
static void wifiReport();
static void handleLine(String ln, bool fromNet = false) {
  ln.trim(); if (!ln.length()) return;
  if (fromNet && !netAuthed) {                 // a network client must pair first
    if (netToken.length() >= 8 && ln == "k " + netToken) { netAuthed = true; net.println("ok paired"); heardFromHost(); }
    else { net.println("err pairing"); net.stop(); }
    return;
  }
  char cmd = ln[0];
  if (cmd == 'w') { if (fromNet) say("err w is USB only"); else setWifi(ln.substring(1)); return; }
  if (cmd == 'W') return wifiReport();
  String arg = ln.substring(1); arg.trim(); arg.toLowerCase();
  heardFromHost();
  if (cmd == 'h') { long s = arg.toInt(); if (s >= 0 && s < 86400) { clockBase = s; clockAt = millis(); } return; }
  if (!A.ok && (cmd == 's' || cmd == 'e' || cmd == 'p')) { say("err lump art missing or bad"); return; }
  if (cmd == 's') {
    if (arg == "demo" || arg == "cycle") { demo = true; demoNext = 0; return reply(); }
    int m = moodIndex(arg);
    if (m < 0) { say("err unknown mood"); return; }
    // repeating the current mood is a no-op, so the bridge can re-assert its mood without
    // restarting the loop or undoing an escalation (alert -> waiting) the board made itself
    // (night is chosen by the bridge, so "s idle" while at night does wake him)
    bool selfEscalated = A.moodName[target] == "waiting" || A.moodName[target] == "longhaul";
    bool same = !demo && (m == target || (selfEscalated && A.base[target] == m));
    demo = false;
    if (!same) setTarget(m);
    return reply();
  }
  if (cmd == 'e') {
    demo = false;
    if (!playEvent(arg)) { say("err unknown event"); return; }
    return reply();
  }
  if (cmd == 'o') {
    int w = 0, a = 0, u = 0;
    sscanf(arg.c_str(), "%d %d %d", &w, &a, &u);
    ovWorking = max(0, w); ovWaiting = max(0, a); ovUnseen = max(0, u);
#ifndef BOARD_ESP32_32E
    lastFrame = -1; // S3 extras are composed over art; 32E counters use the band
#endif
    say("ok overlay working=%d waiting=%d unseen=%d", ovWorking, ovWaiting, ovUnseen);
    return;
  }
#ifdef BOARD_ESP32_32E
  if (cmd == 'P') {
    if (arg.length()) {
      PanelSettings next;
      if (!parsePanelSettings(arg.c_str(), next)) return say("err panel P <invert 0|1> <bgr 0|1> <rotation 0..7>");
      panelSettings = next; tft.tunePanel(next);
      lastFrame = -1; drawBand(millis(), true);
      if (!A.ok) drawRecoveryMessage(); // tuning clears all GRAM; loop cannot restore invalid art
    }
    say("ok panel invert=%u bgr=%u rotation=%u width=%d height=%d", unsigned(panelSettings.invert),
        unsigned(panelSettings.bgr), unsigned(panelSettings.rotation), int(tft.width()), int(tft.height()));
    return;
  }
  if (cmd == 'i') { say("ok touch=xpt2046 pressure=%u peak=%u raw=%ld,%ld irq=%d taps=%lu threshold=%u", touchPressure, touchPeak, long(touchX), long(touchY), digitalRead(PIN_TOUCH_IRQ), (unsigned long)tapCount, tapThreshold); touchPeak = touchPressure; return; }
  if (cmd == 'T') { int t = arg.toInt(); if (t < 1 || t > 4095) return say("err touch threshold 1..4095"); tapThreshold = t; say("ok threshold=%u pressure", tapThreshold); return; }
#else
  if (cmd == 'i') { say("ok imu=%s peak=%.2fg taps=%lu threshold=%.2fg", imuAddr ? "on" : "missing", (float)peakJolt, (unsigned long)tapCount, (float)tapThreshold); return; }
  if (cmd == 'T') { float t = arg.toFloat(); if (t > 0.05f && t < 4) tapThreshold = t; say("ok threshold=%.2fg", (float)tapThreshold); return; }
#endif
  if (cmd == 'c') {
    if (arg.startsWith("#")) arg = arg.substring(1);
    if (arg.length() != 6) { say("err bad colour"); return; }
    long c = strtol(arg.c_str(), nullptr, 16);
    goalR = (c >> 16) & 255; goalG = (c >> 8) & 255; goalB = c & 255;
    say("ok colour %06lX", c);
    return;
  }
  if (cmd == 'X') { tapPending = true; return; }     // pretend the glass was tapped (for testing)
  if (cmd == 'p') { prepMode = arg.toInt() != 0; say("ok prep=%d", prepMode ? 1 : 0); return; }   // meeting countdown running?
  if (cmd == '?') return reply();
  say("err unknown command");
}
static void pollSerial() {
  static String buf;
  while (Serial.available()) { char ch = Serial.read(); if (ch == '\n' || ch == '\r') { if (buf.length()) handleLine(buf); buf = ""; } else if (buf.length() < 240) buf += ch; }
}

// ---------- Wi-Fi ----------
#ifndef LUMP_NO_OTA
static void webBegin();                         // below, with the update server
#endif
static void wifiBegin() {
  Preferences pr; pr.begin("lump", true);
  wifiSsid = pr.getString("ssid", ""); String pass = pr.getString("pass", "");
  netToken = pr.getString("token", ""); tzRule = pr.getString("tz", "");
  pr.end();
  if (!wifiSsid.length()) { WiFi.mode(WIFI_OFF); Serial.println("wifi not set up"); return; }
  WiFi.mode(WIFI_STA); WiFi.setHostname("lump"); WiFi.setAutoReconnect(true);
  WiFi.begin(wifiSsid.c_str(), pass.c_str());
  if (tzRule.length()) configTzTime(tzRule.c_str(), "pool.ntp.org", "time.apple.com");
  server.begin(); server.setNoDelay(true);
#ifndef LUMP_NO_OTA
  webBegin();
#endif
  wifiStarted = true;
  Serial.printf("wifi joining %s\n", wifiSsid.c_str());
}
static void pollWifiStart() {
  static bool attempted = false;
#ifdef BOARD_ESP32_32E
  // STA initialization itself can block, even though association/NTP are
  // asynchronous. Keep it outside the bridge's three-second USB discovery.
  if (millis() < 5000) return;
#endif
  if (!attempted) { attempted = true; wifiBegin(); }
}
// "w <ssid>\t<password>\t<token>\t<tz>" saves and restarts; "w" alone forgets
static void setWifi(String a) {
  if (a.startsWith(" ")) a = a.substring(1);
  Preferences pr; pr.begin("lump", false);
  if (!a.length()) { pr.clear(); pr.end(); say("ok wifi forgotten, restarting"); Serial.flush(); delay(300); ESP.restart(); }
  String f[4]; int n = 0, from = 0;
  for (int i = 0; i <= (int)a.length() && n < 4; i++)
    if (i == (int)a.length() || a[i] == '\t') { f[n++] = a.substring(from, i); from = i + 1; }
  if (n < 3 || !f[0].length() || f[2].length() < 8) { pr.end(); say("err use: w <ssid> TAB <password> TAB <token> [TAB <tz>]"); return; }
  pr.putString("ssid", f[0]); pr.putString("pass", f[1]); pr.putString("token", f[2]);
  if (n >= 4 && f[3].length()) pr.putString("tz", f[3]);
  pr.end();
  say("ok wifi saved ssid=%s, restarting", f[0].c_str()); Serial.flush(); delay(300); ESP.restart();
}
static void wifiReport() {
  if (!wifiStarted) return say("ok wifi=off");
  struct tm t; bool ntp = tzRule.length() && getLocalTime(&t, 0);
  bool up = WiFi.status() == WL_CONNECTED;
  say("ok wifi=%s ssid=%s ip=%s rssi=%d clock=%s client=%s", up ? "connected" : "joining", wifiSsid.c_str(),
      up ? WiFi.localIP().toString().c_str() : "-", up ? WiFi.RSSI() : 0,
      ntp ? "internet" : clockBase >= 0 ? "bridge" : "none", netAuthed && net.connected() ? "paired" : "none");
}
static void pollNet() {
  if (!wifiStarted) return;
  static uint32_t lastCheck = 0; static String buf;
  uint32_t now = millis();
  if (now - lastCheck > 1000) {                // announce lump.local whenever we're on the network
    lastCheck = now;
    bool up = WiFi.status() == WL_CONNECTED;
    if (up && !mdnsUp) { mdnsUp = MDNS.begin("lump"); if (mdnsUp) { MDNS.addService("deskbuddy", "tcp", NET_PORT); Serial.printf("wifi up %s\n", WiFi.localIP().toString().c_str()); } }
    else if (!up && mdnsUp) { MDNS.end(); mdnsUp = false; }
  }
  WiFiClient c = server.accept();
  if (c) { if (net) net.stop(); net = c; net.setNoDelay(true); netAuthed = false; buf = ""; }  // newest wins
  if (!net) return;
  if (!net.connected()) { net.stop(); netAuthed = false; return; }
  while (net.available()) {
    char ch = net.read();
    if (ch == '\n' || ch == '\r') { if (buf.length()) handleLine(buf, true); buf = ""; if (!net) return; }
    else if (buf.length() < 240) buf += ch;
  }
}

#ifndef LUMP_NO_OTA
// ---------- updates over Wi-Fi ----------
static WebServer web(80);
static uint32_t restartAt = 0;
static bool webAuthed() { return netToken.length() >= 8 && web.header("X-Lump-Token") == netToken; }
static String hex(const uint8_t* b, int n) { static const char* H = "0123456789abcdef"; String s; for (int i = 0; i < n; i++) { s += H[b[i] >> 4]; s += H[b[i] & 15]; } return s; }
// SHA-256 of the first len bytes of a partition (len 0 = nothing to compare: empty string)
static String partSha(const esp_partition_t* part, size_t len) {
  if (!part || !len || len > part->size) return "";
  mbedtls_sha256_context c; mbedtls_sha256_init(&c); mbedtls_sha256_starts(&c, 0);
  static uint8_t buf[4096];
  for (size_t off = 0; off < len; off += sizeof buf) {
    size_t n = min(sizeof buf, len - off);
    if (esp_partition_read(part, off, buf, n) != ESP_OK) { mbedtls_sha256_free(&c); return ""; }
    mbedtls_sha256_update(&c, buf, n);
  }
  uint8_t out[32]; mbedtls_sha256_finish(&c, out); mbedtls_sha256_free(&c);
  return hex(out, 32);
}
// shown on the glass while an update streams in (drawn straight to the screen: he isn't animating)
static void progress(const char* what, size_t done, size_t total) {
  static uint32_t last = 0;
  if (done && done != total && millis() - last < 150) return;
  last = millis();
  uint16_t cream = lgfx::color565(255, 241, 220);
  if (!done) { tft.fillScreen(TFT_BLACK); tft.setTextColor(cream); tft.setTextDatum(middle_center); tft.drawString(what, 120, 100); tft.drawRect(30, 125, 180, 14, cream); }
  if (total) tft.fillRect(32, 127, int(176.f * min(done, total) / total), 10, cream);
}
static void onInfo() {
  if (!webAuthed()) return web.send(401, "text/plain", "pairing token needed\n");
  const esp_partition_t* running = esp_ota_get_running_partition();
  String j = "{\"running\":\"" + String(running ? running->label : "?") + "\"";
  j += ",\"app_sha\":\"" + partSha(running, web.arg("app_len").toInt()) + "\"";
  j += ",\"art_sha\":\"" + partSha(artPart, web.arg("art_len").toInt()) + "\"";
  j += ",\"art_ok\":" + String(A.ok ? "true" : "false");
  j += ",\"art_room\":" + String(artPart ? artPart->size : 0);
  const esp_partition_t* next = esp_ota_get_next_update_partition(nullptr);
  j += ",\"app_room\":" + String(next ? next->size : 0) + "}\n";
  web.send(200, "application/json", j);
}
// shared by both uploads: the streaming state of the one in progress
static bool upOk = false; static String upErr; static size_t upSize = 0, upErased = 0;
static mbedtls_sha256_context upSha;
static void upFail(const String& e) { if (upOk) { upOk = false; upErr = e; Serial.printf("update failed: %s\n", e.c_str()); } }
static void onAppData() {
  HTTPRaw& r = web.raw();
  if (r.status == RAW_START) {
    upOk = webAuthed(); upErr = upOk ? "" : "pairing token needed";
    if (!upOk) return;
    upSize = web.header("X-Lump-Size").toInt();
    A.ok = false; progress("updating program", 0, upSize);
    if (!Update.begin(upSize ? upSize : UPDATE_SIZE_UNKNOWN)) return upFail(String("begin: ") + Update.errorString());
    if (web.header("X-Lump-MD5").length() == 32) Update.setMD5(web.header("X-Lump-MD5").c_str());
  } else if (r.status == RAW_WRITE && upOk) {
    if (Update.write(r.buf, r.currentSize) != r.currentSize) return upFail(String("write: ") + Update.errorString());
    progress("updating program", r.totalSize, upSize);
  } else if (r.status == RAW_END && upOk) {
    if (!Update.end(true)) return upFail(String("check: ") + Update.errorString());
  } else if (r.status == RAW_ABORTED) { upFail("upload cut short"); Update.abort(); }
}
static void onArtData() {
  HTTPRaw& r = web.raw();
  if (r.status == RAW_START) {
    upOk = webAuthed(); upErr = upOk ? "" : "pairing token needed";
    if (!upOk) return;
    upSize = web.header("X-Lump-Size").toInt(); upErased = 0;
    if (!artPart || !upSize || upSize > artPart->size) return upFail("art missing size or too big for the partition");
    A.ok = false; progress("updating drawings", 0, upSize);
    mbedtls_sha256_init(&upSha); mbedtls_sha256_starts(&upSha, 0);
  } else if (r.status == RAW_WRITE && upOk) {
    size_t at = r.totalSize - r.currentSize;
    if (at + r.currentSize > upSize) return upFail("more data than X-Lump-Size");
    while (upErased < at + r.currentSize) {        // erase 64 KB blocks just ahead of the data
      size_t n = min<size_t>(0x10000, artPart->size - upErased);
      if (esp_partition_erase_range(artPart, upErased, n) != ESP_OK) return upFail("erase failed");
      upErased += n;
    }
    if (esp_partition_write(artPart, at, r.buf, r.currentSize) != ESP_OK) return upFail("write failed");
    mbedtls_sha256_update(&upSha, r.buf, r.currentSize);
    progress("updating drawings", r.totalSize, upSize);
  } else if (r.status == RAW_END && upOk) {
    uint8_t out[32]; mbedtls_sha256_finish(&upSha, out);
    if (r.totalSize != upSize) return upFail("size mismatch");
    String want = web.header("X-Lump-Sha256"); want.toLowerCase();
    if (want.length() && want != hex(out, 32)) return upFail("sha256 mismatch");
  } else if (r.status == RAW_ABORTED) upFail("upload cut short");
}
static void onUpdateDone() {
  if (upOk) { web.send(200, "text/plain", "ok, restarting\n"); Serial.println("update ok, restarting"); restartAt = millis() + 500; }
  else {
    web.send(upErr == "pairing token needed" ? 401 : 500, "text/plain", "err " + upErr + "\n");
    if (upErr != "pairing token needed") restartAt = millis() + 500;   // the screen was taken over: start clean
  }
}
static void webBegin() {
  static const char* keep[] = {"X-Lump-Token", "X-Lump-Size", "X-Lump-MD5", "X-Lump-Sha256"};
  web.collectHeaders(keep, 4);
  web.on("/info", HTTP_GET, onInfo);
  web.on("/update/app", HTTP_POST, onUpdateDone, onAppData);
  web.on("/update/art", HTTP_POST, onUpdateDone, onArtData);
  web.begin();
}
#endif

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BL, OUTPUT);
#ifdef BOARD_ESP32_32E
  analogWrite(PIN_BL, 0); // hide init, inversion, stale GRAM and diagnostic patterns
  pinMode(PIN_TOUCH_CS, OUTPUT); digitalWrite(PIN_TOUCH_CS, HIGH);
  pinMode(PIN_TOUCH_IRQ, INPUT);
  probePanelGPIO();
  tft.init(); tft.tunePanel(BOARD_PANEL_DEFAULTS);
  tft.panelEvidence();
  drawBand(millis(), true);
#else
  analogWrite(PIN_BL, BL_DAY);
  tft.init(); tft.setRotation(PRISM_MIRROR ? 4 : 0); tft.fillScreen(TFT_BLACK);
#endif
  fb.setColorDepth(16); fb.setPsram(false);
#ifdef BOARD_ESP32_32E
  bool haveBuffer = fb.createSprite(120, 120) != nullptr;
#else
  bool haveBuffer = fb.createSprite(240, 240) != nullptr;
  if (!haveBuffer) { fb.setPsram(true); haveBuffer = fb.createSprite(240, 240) != nullptr; }
#endif
  if (!haveBuffer) { Serial.println("err framebuffer allocation failed"); analogWrite(PIN_BL, BL_DAY); return; }
#ifdef BOARD_ESP32_32E
  fb.fillScreen(TFT_BLACK); fb.drawPixel(0, 0, TFT_RED); fb.drawPixel(1, 0, TFT_GREEN);
  fb.drawPixel(0, 1, TFT_BLUE); fb.drawPixel(1, 1, TFT_WHITE);
  pushFrame();
  const uint16_t expected[] = {TFT_RED, TFT_GREEN, TFT_BLUE, TFT_WHITE};
  unsigned matched = 0;
  for (int y = 0; y < 4; ++y) for (int x = 0; x < 4; ++x) {
    uint16_t got = tft.readPixel(x, y), want = expected[(y / 2) * 2 + x / 2];
    if (got == want) ++matched;
    else Serial.printf("scaled pixel %d,%d: %04X expected %04X\n", x, y, unsigned(got), unsigned(want));
  }
  Serial.printf("scaled sprite readback %s (%u/16 pixels)\n", matched == 16 ? "PASS" : "FAIL", matched);
#endif
  if (!mapArt() || !parseAnim()) {
    A.ok = false;
    Serial.printf("err lump art missing or bad (send it: %s)\n", ART_RECOVERY_COMMAND);
    drawRecoveryMessage();
    analogWrite(PIN_BL, BL_DAY);
    return;
  }
  Serial.printf("desk-buddy lump ready: %u moods, %u sequences, %u frames\n", A.nMoods, A.nSeqs, A.nFrames);
  randomSeed(esp_random());
#ifdef BOARD_ESP32_32E
  Serial.printf("touch XPT2046 ready: CS=%d IRQ=%d threshold=%u; OTA disabled\n", PIN_TOUCH_CS, PIN_TOUCH_IRQ, tapThreshold);
#else
  if (imuInit()) { xTaskCreatePinnedToCore(imuTask, "imu", 4096, nullptr, 2, nullptr, 0); Serial.println("imu ready"); }
  else Serial.println("imu not found");
#endif
  curMood = target = 0; startSeq(A.loopOf[0]); loopSince = millis();
#ifdef BOARD_ESP32_32E
  // Replace the scaled-pixel diagnostic with real art before lighting up.
  lastFrame = rd16(A.seq[curSeq]); decodeFrame(lastFrame); pushFrame();
  analogWrite(PIN_BL, BL_DAY);
#endif
  Serial.printf("heap free=%u largest=%u psram=%u\n", unsigned(ESP.getFreeHeap()), unsigned(ESP.getMaxAllocHeap()), unsigned(ESP.getPsramSize()));
}

void loop() {
  pollSerial();
  pollWifiStart();                               // also runs when art/buffer setup failed
  pollNet();
#ifndef LUMP_NO_OTA
  if (wifiStarted) web.handleClient();
  if (restartAt && millis() > restartAt) ESP.restart();
#endif
  if (!A.ok) { delay(20); return; }
#ifdef BOARD_ESP32_32E
  pollTouch(millis());
#endif
  if (tapPending) {
    tapPending = false;
#ifdef BOARD_ESP32_32E
    touchFeedback = true; touchFeedbackAt = millis();
    drawBand(touchFeedbackAt);                      // visible before starting the gag
#endif
    say("tap");                                    // the bridge decides what a tap means for Claude
    playEvent("tap");                              // he reacts in every mood (the bridge may then move him on)
  }
  uint32_t now = millis();                         // read after the tap: a gag started this pass must not look old
  runDemo(now);
  runOffline(now);
  autoBehaviour(now);
  backlight(now);
#ifdef BOARD_ESP32_32E
  drawBand(now);
#endif

  int32_t age = int32_t(now - seqStart);         // signed: something started after 'now' is age 0, not 49 days
  uint32_t tick = age > 0 ? uint32_t(age) * A.fps / 1000 : 0;
  if (A.kind[curSeq] != LOOP && tick >= A.len[curSeq]) { settle(A.to[curSeq]); tick = 0; }
  uint32_t pos = A.kind[curSeq] == LOOP ? tick % A.len[curSeq] : min<uint32_t>(tick, A.len[curSeq] - 1);
  uint16_t idx = rd16(A.seq[curSeq] + 2 * pos);
  // fade the body colour toward its goal (~1 s), redrawing as it changes
  static uint32_t lastFade = 0;
  if (now - lastFade >= 30 && (fabsf(goalR - tintR) + fabsf(goalG - tintG) + fabsf(goalB - tintB)) > 1.5f) {
    lastFade = now;
    tintR += (goalR - tintR) * 0.12f; tintG += (goalG - tintG) * 0.12f; tintB += (goalB - tintB) * 0.12f;
    lastFrame = -1;
  }
  static uint32_t lastOverlayTick = 0;
  uint32_t overlayTick = now / 100;              // corner extras animate at ~10 fps
#ifdef BOARD_ESP32_32E
  bool ov = false; // the separate status band never needs an art redraw
#else
  bool ov = overlayVisible();
#endif
  if (idx != lastFrame || (ov && overlayTick != lastOverlayTick)) {
    lastOverlayTick = overlayTick;
    decodeFrame(idx);
#ifdef BOARD_ESP32_32E
    pushFrame();
#else
    if (ov) drawOverlay(now);
    fb.pushSprite(0, 0);
#endif
    lastFrame = idx;
    static uint32_t t0 = now; static int n = 0;
    if (++n, now - t0 >= 2000) { fps = n * 1000.f / (now - t0); n = 0; t0 = now; }
  }
  delay(2);
}
