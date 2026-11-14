// Compile the production parser, player, board and serial loop against inert hardware.
#define BOARD_ESP32_32E 1
#define LUMP_NO_OTA 1
#include "../../firmware/src/main.cpp"
#include <fstream>
#include <iostream>

static void require(bool good, const char* why) { if (!good) throw std::runtime_error(why); }
int main(int argc, char** argv) {
  require(argc >= 2, "need scenario");
  std::string scenario = argv[1];
  if (scenario == "touch") {
    int32_t x, y;
    for (int i = 0; i < 100; ++i) tft.rawTouch(&x, &y);
  } else if (scenario == "panel-parse") {
    for (int invert = 0; invert < 2; ++invert) for (int bgr = 0; bgr < 2; ++bgr) for (int r = 0; r < 8; ++r) {
      std::string args = std::to_string(invert) + "\t" + std::to_string(bgr) + " " + std::to_string(r);
      PanelSettings s{};
      require(parsePanelSettings(args.c_str(), s), "valid panel settings rejected");
      require(s.invert == bool(invert) && s.bgr == bool(bgr) && s.rotation == r, "panel fields parsed incorrectly");
    }
    for (const char* args : {"", "1", "1 0", "1 0 8", "2 0 0", "1 2 0", "-1 0 0", "1 0 -1",
                            "1 0 00", "1 0 0x", "1 0 0 1", "1 0 99999999999999999999999999", "true 0 0"}) {
      PanelSettings s = {true, false, 3};
      require(!parsePanelSettings(args, s), "malformed panel settings accepted");
      require(s.invert && !s.bgr && s.rotation == 3, "invalid parsing changed settings");
    }
  } else {
    require(argc >= 3, "need art file");
    std::ifstream file(argv[2], std::ios::binary);
    hostArt.assign(std::istreambuf_iterator<char>(file), {});
    hostWifiConfigured = scenario == "boot-configured" || scenario == "boot-no-ntp" || scenario == "boot-online";
    hostWifiConnected = scenario == "boot-no-ntp" || scenario == "boot-online";
    hostNtpAvailable = scenario == "boot-online";
    std::fill(tft.pixels.begin(), tft.pixels.end(), 0x1357); // stale display memory
    setup();
    require(!hostBacklight.empty() && hostBacklight.front() == 0 && hostBacklight.back() == BL_DAY,
            "boot did not hide panel init/diagnostics then enable backlight");
    require(Serial.output.find("panel native mapping PASS") != std::string::npos,
            "logical/native asymmetric mapping failed");
    require(Serial.output.find("panel full clear PASS") != std::string::npos, "native GRAM clear failed");
    if (scenario == "recovery-tuning") {
      require(!A.ok, "missing/corrupt art accepted");
      int oldSeq = curSeq;
      for (int r = 0; r < 8; ++r) {
        tft.ops.clear(); Serial.output.clear();
        Serial.input = "P 1 0 " + std::to_string(r) + "\n?\ns demo\nW\n";
        loop();
        bool message = false, instructions = false, band = false;
        for (const auto& op : tft.ops) {
          message |= op.text == "no drawings yet";
          instructions |= op.text == "tools/flash.sh esp32-32e";
          if (op.name == "push") {
            BandLayout b = bandLayout(tft.width(), tft.height());
            require(op.x == b.x && op.y == b.y && op.w == b.w && op.h == b.h,
                    "animation pushed with invalid art");
            band = true;
          }
        }
        require(message && instructions, "tuning erased recovery instructions");
        require(band, "tuning failed to restore recovery status band");
        require(!A.ok && curSeq == oldSeq && !demo, "tuning enabled animation with invalid art");
        require(Serial.output.find("ok panel invert=1 bgr=0 rotation=" + std::to_string(r)) != std::string::npos,
                "recovery tuning stopped replying");
        require(Serial.output.find("err lump art missing or bad") != std::string::npos &&
                Serial.output.find("ok wifi=off") != std::string::npos, "recovery commands stopped replying");
      }
    } else if (scenario == "bad-art") {
      require(!A.ok, "damaged mood name accepted as playable art");
      require(Serial.output.find("err lump art missing or bad") != std::string::npos, "missing clean boot rejection");
      Serial.output.clear();
      int oldSeq = curSeq;
      Serial.input = "?\ns alert\ns demo\ne tap\nW\ni\n";
      loop();
      hostNow += 600000; loop();
      require(curSeq == oldSeq && !demo, "animation advanced with damaged art");
      require(Serial.output.find("err lump art missing or bad") != std::string::npos, "missing art command error");
      require(Serial.output.find("ok wifi=off") != std::string::npos, "serial W stopped answering");
      require(Serial.output.find("ok touch=xpt2046") != std::string::npos, "serial i stopped answering");
    } else if (scenario == "bounds") {
      require(A.ok, "valid art rejected");
      int oldTarget = target;
      setTarget(-1); require(target == oldTarget, "negative target accepted");
      setTarget(A.nMoods); require(target == oldTarget, "out of range target accepted");
      require(nextHop(-1, 0) == -1 && nextHop(0, A.nMoods) == -1, "invalid routing accepted");
      // Simulate a failed internal lookup despite validation: demo and escalation
      // must leave a valid target even if the name disappears after parsing.
      A.moodName[moodIndex("waiting")] = "vaiting";
      int alert = moodIndex("alert"); target = curMood = alert; startSeq(A.loopOf[alert]);
      loopSince = 0; hostNow = ESCALATE_MS + 1; autoBehaviour(hostNow);
      require(target == alert, "escalation used a failed lookup");
      demo = true; demoStep = 7; demoNext = 0; runDemo(hostNow);
      require(target == alert, "demo used a failed lookup");
      // A failed reparse must also clear an earlier successful A.ok.
      art = hostArt.data(); artSize = hostArt.size();
      hostArt[0] = 'X'; require(!parseAnim() && !A.ok, "failed reparse left art enabled");
    } else if (scenario == "band") {
      require(A.ok, "valid art rejected");
      require(tft.width() == 240 && tft.height() == 320 && tft.getRotation() == 0, "boot is not portrait rotation 0");
      require(tft.hostPanel->state.memory_width == 320 && tft.hostPanel->state.memory_height == 240
              && tft.hostPanel->state.offset_rotation == 5 && tft.hostPanel->madctl == 0x20,
              "native geometry/rotation offset differs from panel defaults");
      require(tft.inverted && tft.hostPanel->state.rgb_order, "optical defaults not INVON/RGB");
      require(std::find(tft.pixels.begin(), tft.pixels.end(), 0x1357) == tft.pixels.end(), "boot left stale GRAM");
      hostNow = 10000;
      for (int r = 0; r < 8; ++r) {
        std::string cmd = "P 1 0 " + std::to_string(r);
        Serial.output.clear(); handleLine(cmd.c_str());
        require(Serial.output.find("ok panel invert=1 bgr=0 rotation=" + std::to_string(r)) != std::string::npos,
                "P reply missing applied settings");
        require(lastFrame == -1, "panel tuning did not invalidate art");
        std::fill(tft.pixels.begin(), tft.pixels.end(), 0x1357);
        int madctl = tft.hostPanel->madctl;
        tft.clearGRAM();
        require(tft.hostPanel->madctl == madctl && tft.getRotation() == r, "native clear changed live rotation");
        require(std::all_of(tft.pixels.begin(), tft.pixels.end(), [](uint16_t c) { return c == 0; }),
                "native clear missed physical GRAM at a tuning rotation");
        // Fill only the band with stale pixels, preserving a sentinel in the art.
        BandLayout b = bandLayout(tft.width(), tft.height());
        require(b.w*b.h == 240*80 && (r & 1 ? b.x == 240 && b.y == 0 : b.x == 0 && b.y == 240), "band geometry wrong");
        tft.fillScreen(0x2468); tft.fillRect(b.x, b.y, b.w, b.h, 0x1357);
        tft.ops.clear(); bandFb.ops.clear(); offline = true; demo = false; touchFeedback = true; touchFeedbackAt = hostNow;
        ovWorking = 0; ovWaiting = 999; ovUnseen = 1000000;
        drawBand(hostNow, true);
        for (int y = 0; y < tft.height(); ++y) for (int x = 0; x < tft.width(); ++x)
          require(tft.readPixel(x, y) == (x < 240 && y < 240 ? 0x2468 : TFT_BLACK), "band failed to clear or covered art");
        require(tft.ops.size() == 1 && tft.ops[0].name == "push" && tft.ops[0].x == b.x && tft.ops[0].y == b.y
                && tft.ops[0].w == b.w && tft.ops[0].h == b.h, "band was not pushed once as a complete image");
        for (const auto& op : bandFb.ops) require(op.x >= 0 && op.y >= 0 && op.x+op.w <= b.w && op.y+op.h <= b.h,
                                                 "band primitive overlaps art or panel edge");
        for (const char* text : {"WORK", "WAIT", "DONE", "0", "999", "999+", "OFF", "TAP"}) {
          bool found = false; for (const auto& op : bandFb.ops) found |= op.text == text;
          require(found, "band omitted count, label or touch acknowledgment");
        }
      }
      handleLine("P 0 1 7");
      require(!tft.inverted && !tft.hostPanel->state.rgb_order && tft.getRotation() == 7, "P settings not applied to hardware");
      handleLine("P 1 0 0");
      Serial.output.clear(); handleLine("P 1 0 8");
      require(Serial.output.find("err panel") != std::string::npos && panelSettings.rotation == 0, "bad P mutated panel");
      Serial.output.clear(); handleLine("P");
      require(Serial.output.find("ok panel invert=1 bgr=0 rotation=0 width=240 height=320") != std::string::npos, "P query changed defaults");
      require(!prepMode, "uppercase P changed lowercase meeting prep");
      handleLine("p 1"); require(prepMode, "lowercase p stopped working");
    } else if (scenario == "band-redraw") {
      hostNow = 1000; drawBand(hostNow, true);
      auto unchanged = [&](uint32_t now) {
        tft.ops.clear(); bandFb.ops.clear(); drawBand(now);
        require(tft.ops.empty() && bandFb.ops.empty(), "unchanged band was redrawn");
      };
      auto changed = [&](uint32_t now) {
        tft.ops.clear(); bandFb.ops.clear(); drawBand(now);
        require(tft.ops.size() == 1 && tft.ops[0].name == "push", "change erased live panel or pushed more than once");
        require(!bandFb.ops.empty(), "changed band was not composed off-screen");
      };
      unchanged(1002); unchanged(7999); // across many old 100ms redraw ticks
      int frame = lastFrame;
      handleLine("o 2 1 3"); require(lastFrame == frame, "counter change invalidated art");
      changed(7999); unchanged(7999); // a content change is immediate, within same tick
      ovUnseen = 1000000; changed(7999);
      ovUnseen = 999999; unchanged(7999); // both render as 999+
      offline = true; changed(8001); unchanged(9000); // OFF appears once
      touchFeedback = true; touchFeedbackAt = 9000;
      changed(9000); unchanged(9449); changed(9450); unchanged(10000);
      require(!touchFeedback, "TAP did not disappear at exactly 450ms");
      offline = false; touchFeedback = true; touchFeedbackAt = UINT32_MAX-100;
      changed(UINT32_MAX-100); unchanged(200); changed(349); // millis wrap
      handleLine("P 1 0 2"); // same band size, but panel was cleared: force re-push
      require(tft.ops.back().name == "push", "tuning failed to restore cached band");
    } else if (scenario == "offline-counts") {
      hostNow = 1000; handleLine("o 2 1 3");
      hostNow += HOST_TIMEOUT_MS + 1; runOffline(hostNow); drawBand(hostNow);
      require(offline && !demo && ovWorking == 2 && ovWaiting == 1 && ovUnseen == 3,
              "closed-port counter snapshot did not survive host timeout");
      bool off = false; for (const auto& op : bandFb.ops) off |= op.text == "OFF";
      require(off, "retained snapshot is missing OFF marker");
    } else if (scenario == "touch-feedback") {
      hostNow = 1000; lgfx::spi::hostPressed = true;
      pollTouch(hostNow); hostNow += 40; pollTouch(hostNow);
      require(tapPending && tapCount == 1, "physical press not accepted");
      Serial.output.clear(); bandFb.ops.clear(); loop();
      require(touchFeedback && Serial.output.find("tap\n") != std::string::npos && Serial.output.find("gag tap_") != std::string::npos,
              "accepted press missing feedback/tap/gag");
      bool ring = false; for (const auto& op : bandFb.ops) ring |= op.name == "circle";
      require(ring && !demo, "ring missing or touch enabled demo");
      hostNow += 1000; loop(); require(tapCount == 1 && !touchFeedback, "held press repeated or feedback failed to expire");
      lgfx::spi::hostPressed = false; hostNow += 20; loop(); hostNow += 80; loop();
      lgfx::spi::hostPressed = true; hostNow += 20; loop(); hostNow += 40; loop();
      require(tapCount == 2 && touchFeedback, "release/repress failed to rearm feedback");
    } else {
      require(A.ok, "valid art rejected");
      require(hostRadioCalls == 0, "radio started before USB discovery");
      Serial.output.clear(); Serial.input = "?\n"; loop();
      require(hostNow < 3000 && Serial.output.find("ok mode=") != std::string::npos, "USB missed discovery deadline");
      hostNow = 2999; loop(); require(hostRadioCalls == 0, "radio blocked discovery window");
      hostNow = 5000; loop(); require(hostRadioCalls > 0, "deferred radio never started");
      Serial.output.clear(); Serial.input = "?\nW\n"; loop();
      require(Serial.output.find("ok mode=") != std::string::npos, "serial blocked by unavailable network/NTP");
      if (hostWifiConnected) {
        require(Serial.output.find("ok wifi=connected") != std::string::npos, "AP status incorrect");
        require(Serial.output.find(hostNtpAvailable ? "clock=internet" : "clock=none") != std::string::npos, "NTP status incorrect");
      }
    }
  }
  std::cout << "PASS " << scenario << "\n";
}
