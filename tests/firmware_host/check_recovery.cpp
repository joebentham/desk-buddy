// Exercise the production recovery screen and boot message for either board.
#define LUMP_NO_OTA 1
#include "../../firmware/src/main.cpp"
#include <fstream>
#include <iostream>

int main(int argc, char** argv) {
  assert(argc == 3);
  std::ifstream file(argv[1], std::ios::binary);
  hostArt.assign(std::istreambuf_iterator<char>(file), {});
  setup();
  assert(!A.ok);
  std::string command = argv[2];
  assert(Serial.output.find("err lump art missing or bad (send it: " + command + ")\n") != std::string::npos);
  bool message = false, instructions = false;
  for (const auto& op : tft.ops) {
    message |= op.text == "no drawings yet";
    instructions |= op.text == command;
  }
  assert(message && instructions);
  std::cout << "PASS recovery\n";
}
