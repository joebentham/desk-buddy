"""The bridge's stall watchdog (StallWatch): a frozen bridge leaves every thread's stack in stalls.log.

    python -m unittest discover -s tests -v
"""
import os, tempfile, time, unittest

from test_bridge import bridge


class StallWatchTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="deskbuddy-stall-"), "stalls.log")

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_a_stall_dumps_every_threads_stack(self):
        w = bridge.StallWatch(path=self.path, secs=1)
        self.addCleanup(w.stop)
        w.arm()
        time.sleep(1.8)                                   # the loop doesn't come round in time
        w.stop()
        text = self.read()
        self.assertRegex(text, r"--- \d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
        self.assertIn("test_a_stall_dumps_every_threads_stack", text)    # this thread, caught mid-sleep
        self.assertIn("Thread", text)

    def test_a_loop_that_keeps_coming_round_dumps_nothing(self):
        w = bridge.StallWatch(path=self.path, secs=1)
        self.addCleanup(w.stop)
        for _ in range(4):
            w.arm()
            time.sleep(0.3)
        w.stop()
        self.assertNotIn("Thread", self.read())

    def test_how_long_it_was_stuck_goes_in_the_bridge_log(self):
        t = [1000.0]
        w = bridge.StallWatch(path=self.path, secs=15, clock=lambda: t[0])
        self.addCleanup(w.stop)
        w.arm()
        t[0] += 1
        with self.assertNoLogs("bridge", "INFO"):
            w.arm()
        t[0] += 42
        with self.assertLogs("bridge", "INFO") as logs:
            w.arm()
        self.assertIn("stuck for 42 s", logs.output[0])

    def test_time_marks_are_at_most_one_a_minute(self):
        t = [1000.0]
        w = bridge.StallWatch(path=self.path, secs=15, clock=lambda: t[0])
        self.addCleanup(w.stop)
        for _ in range(130):                              # 130 s of normal one-second rounds
            w.arm(); t[0] += 1
        w.stop()
        self.assertEqual(self.read().count("---"), 3)


if __name__ == "__main__":
    unittest.main()
