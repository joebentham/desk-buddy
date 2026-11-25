// Lump on the Mac: a small visual reminder that mirrors the desk Lump's mood.
//
// By default he only shows while you're away from the desk: he hides while your desk monitor is
// plugged in and the desk Lump is online (the Mac can't sense distance, but the desk Lump sits by a
// monitor, so "laptop on that monitor" is a good stand-in for "you're at your desk").
// Right-click him to pick your desk monitor (default: any external display) or to show him all the time.
// While he's hidden, open Lump again: he shows with his menu open, and hides again once it closes.
// On a MacBook with a notch he sits in a little black wing just right of the camera.
// With no notch screen he's a menu bar item instead.
// No pop-outs, no text: notifications already do that. Right-click him for options.
// Click him to open what he's on about (the bridge picks it: GET /open): the thread that needs you,
// the one that's working, what just finished, a VIP's Slack DM, or the meeting that's starting.
//
// Mood and colour come from the bridge (GET 127.0.0.1:47823/status, once a second), and so do one-off
// animations it plays on the desk Lump ("event"): any it has drawings for, like the Slack paper plane, play once.
// Frames come from tools/lumpbar/bake.py (copied into the app's Resources/frames).
// Build and launch: zsh tools/lumpbar/build.sh

import Cocoa

let STATUS_URL = URL(string: "http://127.0.0.1:47823/status")!
let OPEN_URL = URL(string: "http://127.0.0.1:47823/open")!
let CLAUDE_ID = "com.anthropic.claudefordesktop"
let DEFAULT_COLOUR = "FF7A45"
let PEEK: TimeInterval = 60             // opening the app shows him (menu open) at most this long, even at the desk
let LINGER: TimeInterval = 2            // after that menu closes, he stays this long before hiding again
let DEVICE_GRACE: TimeInterval = 60     // the desk Lump has to be gone this long before he counts as away (OTA, Wi-Fi blips)

// Sharing the notch with the Matchday island (the Matchday project's island/ app), over distributed notifications:
// he posts LUMP_WING ["width": n] when his wing shows (0 when it hides); while its island is up, Matchday widens
// the pill's right side by that much and posts ISLAND_SLOT with the slot's global rect ["x","y","w","h"]
// ([:] when the island goes down). He moves into the slot, above the island, and draws no wing of his own there.
let LUMP_WING = Notification.Name("com.deskbuddy.lump.wing")
let ISLAND_SLOT = Notification.Name("local.matchday.island.slot")
let ISLAND_HELLO = Notification.Name("local.matchday.island.hello")
let MATCHDAY_ID = "local.matchday.island"

// MARK: - frames

/// The baked strips for one mood and ink, as premultiplied RGBA, painted any body colour on demand.
/// Every pixel is linear in the body colour: out = k + (w - k) * colour / 255 (k: body black, w: body white).
final class Strip {
    let k: [UInt8], w: [UInt8], width: Int, height: Int
    init?(k kURL: URL, w wURL: URL) {
        guard let a = Strip.pixels(kURL), let b = Strip.pixels(wURL), a.w == b.w, a.h == b.h else { return nil }
        k = a.px; w = b.px; width = a.w; height = a.h
    }
    static func pixels(_ url: URL) -> (px: [UInt8], w: Int, h: Int)? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return nil }
        let w = img.width, h = img.height
        var px = [UInt8](repeating: 0, count: w * h * 4)
        let ok = px.withUnsafeMutableBytes { buf -> Bool in
            guard let ctx = CGContext(data: buf.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                      space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h)); return true
        }
        return ok ? (px, w, h) : nil
    }
    /// Every frame of the strip with the body painted `rgb` (frames are `frameWidth` px wide, side by side).
    func painted(_ rgb: (Int, Int, Int), frameWidth: Int) -> [CGImage] {
        var out = [UInt8](repeating: 0, count: k.count)
        let t = [rgb.0, rgb.1, rgb.2]
        for i in stride(from: 0, to: k.count, by: 4) {
            for c in 0..<3 { out[i + c] = UInt8(max(0, min(255, Int(k[i + c]) + (Int(w[i + c]) - Int(k[i + c])) * t[c] / 255))) }
            out[i + 3] = k[i + 3]
        }
        guard let provider = CGDataProvider(data: Data(out) as CFData),
              let full = CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
                                 space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                 bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                                 provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent) else { return [] }
        return (0..<(width / frameWidth)).compactMap { full.cropping(to: CGRect(x: $0 * frameWidth, y: 0, width: frameWidth, height: height)) }
    }
}

final class Sprites {
    struct Mood: Decodable { let frames: Int; let ticks: [Int] }
    /// sizes: "wing" (beside the notch) and "item" (a menu bar item), each [width, height] in px, drawn for that size.
    struct Manifest: Decodable { let fps: Int; let sizes: [String: [Int]]; let moods: [String: Mood]; let events: [String: Mood]? }
    let manifest: Manifest, dir: URL
    private var strips: [String: Strip] = [:]
    private var painted: [String: [CGImage]] = [:]

    init?(dir: URL) {
        guard let data = try? Data(contentsOf: dir.appendingPathComponent("manifest.json")),
              let m = try? JSONDecoder().decode(Manifest.self, from: data) else { return nil }
        guard m.sizes["wing"]?.count == 2, m.sizes["item"]?.count == 2 else { return nil }
        manifest = m; self.dir = dir
    }
    /// His slot's width over its height (the same at every size).
    var aspect: CGFloat { let s = manifest.sizes["wing"]!; return CGFloat(s[0]) / CGFloat(s[1]) }
    func ticks(_ mood: String) -> [Int] { manifest.moods[mood]?.ticks ?? manifest.events?[mood]?.ticks ?? [] }
    func frames(_ mood: String, size: String, ink: String, colour: String) -> [CGImage] {
        let key = "\(mood)-\(size)-\(ink)-\(colour)"
        if let f = painted[key] { return f }
        let sk = "\(mood)-\(size)-\(ink)"
        if strips[sk] == nil {
            strips[sk] = Strip(k: dir.appendingPathComponent("\(sk)-k.png"), w: dir.appendingPathComponent("\(sk)-w.png"))
        }
        guard let strip = strips[sk] else { return [] }
        if painted.count > 24 { painted.removeAll() }      // the meeting countdown slides the colour: don't hoard
        let f = strip.painted(rgb(colour), frameWidth: manifest.sizes[size]?[0] ?? strip.height)
        painted[key] = f
        return f
    }
    func rgb(_ hex: String) -> (Int, Int, Int) {
        let v = Int(hex, radix: 16) ?? 0xFF7A45
        return ((v >> 16) & 255, (v >> 8) & 255, v & 255)
    }
}

// MARK: - views

/// The black wing that sticks out of the right side of the notch, with Lump in it.
final class WingView: NSView {
    var image: CGImage? { didSet { needsDisplay = true } }
    var overlap: CGFloat = 12        // how far the wing reaches under the notch, to fill its rounded corner
    var dim: CGFloat = 1 { didSet { needsDisplay = true } }
    /// Sitting in the Matchday island's slot: the island is the black behind him, so draw only Lump.
    var docked = false { didSet { needsDisplay = true } }
    var onRightClick: ((NSEvent) -> Void)?, onClick: (() -> Void)?

    override func draw(_ dirtyRect: NSRect) {
        let b = bounds, r = min(10, b.height * 0.3)
        if !docked {
            let p = NSBezierPath()
            p.move(to: NSPoint(x: 0, y: b.maxY)); p.line(to: NSPoint(x: b.maxX, y: b.maxY))
            p.line(to: NSPoint(x: b.maxX, y: r))
            p.appendArc(withCenter: NSPoint(x: b.maxX - r, y: r), radius: r, startAngle: 0, endAngle: 270, clockwise: true)
            p.line(to: NSPoint(x: 0, y: 0)); p.close()
            NSColor.black.setFill(); p.fill()
        }
        guard let img = image, let ctx = NSGraphicsContext.current?.cgContext else { return }
        let h = b.height - 2, w = h * CGFloat(img.width) / CGFloat(img.height), x = overlap + (b.width - overlap - w) / 2
        ctx.interpolationQuality = .high
        ctx.setAlpha(dim)
        ctx.draw(img, in: CGRect(x: x, y: 1, width: w, height: h))
    }
    override func rightMouseDown(with event: NSEvent) { onRightClick?(event) }
    override func mouseDown(with event: NSEvent) {
        if event.modifierFlags.contains(.control) { onRightClick?(event) } else { onClick?() }
    }
}

// MARK: - app

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var sprites: Sprites!
    var mood = "idle", colour = DEFAULT_COLOUR, online = false
    /// A one-off animation playing once over his mood (e.g. tap_plane for a Slack DM), and the bridge's
    /// count of the last one it reported (-1 until the first poll, so an old one isn't replayed at launch).
    var playing: String?, lastEventN = -1
    var deviceSeen = Date.distantPast, atDesk = false, placed = false
    var tick = 0, shownKey = "", peekUntil = Date.distantPast, menuWhenPlaced = false
    var wing: NSPanel?, wingView: WingView?
    var item: NSStatusItem?
    /// The slot the Matchday island keeps for him while it's up (global coordinates); nil = use his own wing.
    var dockRect: NSRect?
    /// The wing width last told to Matchday, so it's only re-posted when it changes.
    var announced: CGFloat?
    let prefs = UserDefaults.standard
    var preferMenuBar: Bool { get { prefs.bool(forKey: "menuBarOnly") } set { prefs.set(newValue, forKey: "menuBarOnly") } }
    var showAlways: Bool { get { prefs.bool(forKey: "showAlways") } set { prefs.set(newValue, forKey: "showAlways") } }
    /// The monitor the desk Lump lives by, as macOS names it; nil = any external display.
    /// LUMPBAR_DESK_DISPLAY overrides it (for testing "away" while sitting at the desk).
    var deskDisplay: String? {
        get { ProcessInfo.processInfo.environment["LUMPBAR_DESK_DISPLAY"] ?? prefs.string(forKey: "deskDisplay") }
        set { prefs.set(newValue, forKey: "deskDisplay") }
    }
    lazy var theMenu: NSMenu = { let m = NSMenu(); m.delegate = self; return m }()

    func applicationDidFinishLaunching(_ n: Notification) {
        guard let dir = Bundle.main.url(forResource: "frames", withExtension: nil), let s = Sprites(dir: dir) else {
            NSLog("lumpbar: no frames in the app bundle (run tools/lumpbar/bake.py, then build.sh)"); NSApp.terminate(nil); return
        }
        sprites = s                 // placed after the first poll, so he doesn't flash up at the desk
        if let o = ProcessInfo.processInfo.environment["LUMPBAR_OPEN"] {   // for testing a click: "<url>|<account>"
            let parts = o.components(separatedBy: "|")
            open(url: URL(string: parts[0]), account: parts.count > 1 ? parts[1] : nil)
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { exit(0) }
            return
        }
        if ProcessInfo.processInfo.environment["LUMPBAR_PRINT_MENU"] != nil {   // for checking the menu without clicking
            menuNeedsUpdate(theMenu)
            func dump(_ m: NSMenu, _ pad: String) {
                for it in m.items {
                    print(pad + (it.isSeparatorItem ? "----" : (it.state == .on ? "✓ " : "  ") + it.title + (it.isEnabled ? "" : "  (info)")))
                    if let sub = it.submenu { sub.delegate?.menuNeedsUpdate?(sub); dump(sub, pad + "    ") }
                }
            }
            dump(theMenu, ""); exit(0)
        }
        NotificationCenter.default.addObserver(self, selector: #selector(place), name: NSApplication.didChangeScreenParametersNotification, object: nil)
        let dnc = DistributedNotificationCenter.default()
        dnc.addObserver(forName: ISLAND_SLOT, object: nil, queue: .main) { [weak self] n in self?.slotChanged(n.userInfo) }
        dnc.addObserver(forName: ISLAND_HELLO, object: nil, queue: .main) { [weak self] _ in self?.announce(force: true) }
        let anim = Timer(timeInterval: 1.0 / Double(s.manifest.fps), repeats: true) { [weak self] _ in self?.step() }
        RunLoop.main.add(anim, forMode: .common)
        let poll = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.poll() }
        RunLoop.main.add(poll, forMode: .common)
        if !prefs.bool(forKey: "launchedBefore") {      // first launch: let a new user see where he lives
            prefs.set(true, forKey: "launchedBefore"); peekUntil = Date().addingTimeInterval(PEEK); menuWhenPlaced = true
        }
        poll.fire()
    }

    /// Opening the app while it's running (Finder, Spotlight, `open`): show him with his menu open, even at the desk.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        peekUntil = Date().addingTimeInterval(PEEK); place(); showMenu(); return false
    }

    /// Drop his menu down from wherever he is (after he's had a moment to appear).
    func showMenu() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            NSApp.activate(ignoringOtherApps: true)
            if self.item != nil { self.itemMenu() }
            else if let v = self.wingView, self.wing?.isVisible == true {
                self.theMenu.popUp(positioning: nil, at: NSPoint(x: v.bounds.maxX - v.bounds.height / 2, y: -4), in: v)
            }
        }
    }
    /// The menu closed: if he only came up because you opened the app, he can go again.
    func menuDidClose(_ menu: NSMenu) {
        guard Date() < peekUntil else { return }
        peekUntil = Date().addingTimeInterval(LINGER)
        DispatchQueue.main.asyncAfter(deadline: .now() + LINGER + 0.1) { if self.computeAtDesk() != self.atDesk { self.place() } }
    }

    // MARK: where he lives

    /// The built-in screen with a notch, if one is on: (screen, menu bar height, global x of the notch's right edge).
    /// LUMPBAR_FAKE_NOTCH=1 pretends the main screen has a 200 pt notch, for trying the wing on any Mac.
    func notch() -> (screen: NSScreen, height: CGFloat, right: CGFloat)? {
        if ProcessInfo.processInfo.environment["LUMPBAR_FAKE_NOTCH"] != nil, let s = NSScreen.main {
            return (s, s.frame.maxY - s.visibleFrame.maxY, s.frame.midX + 100)
        }
        guard let s = NSScreen.screens.first(where: { $0.safeAreaInsets.top > 0 && $0.auxiliaryTopRightArea != nil }) else { return nil }
        let f = s.frame
        var right = s.auxiliaryTopRightArea!
        // that rect may be relative to the screen or global: it always runs to the screen's right edge
        if abs(right.maxX - f.width) < 1 && abs(right.maxX - f.maxX) >= 1 { right.origin.x += f.minX }
        return (s, s.safeAreaInsets.top, right.minX)
    }

    func externalDisplays() -> [String] {
        NSScreen.screens.filter { s in
            CGDisplayIsBuiltin(s.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID ?? 0) == 0
        }.map { $0.localizedName }
    }
    func deskDisplayConnected() -> Bool {
        let shown = externalDisplays()
        if let want = deskDisplay { return shown.contains(want) }
        return !shown.isEmpty
    }
    /// At the desk = desk monitor plugged in and the desk Lump heard from in the last minute.
    func computeAtDesk() -> Bool {
        !showAlways && Date() >= peekUntil && deskDisplayConnected() && Date().timeIntervalSince(deviceSeen) < DEVICE_GRACE
    }

    @objc func place() {
        defer { announce() }
        atDesk = computeAtDesk()
        if atDesk { hideWing(); hideItem(); shownKey = ""; return }   // the real one is right there
        if !preferMenuBar, let n = notch() { showWing(n); hideItem() } else { hideWing(); showItem() }
        shownKey = ""
    }

    /// Tell the Matchday island how much room he needs beside it (0 = not in the notch).
    func announce(force: Bool = false) {
        let width: CGFloat = wing?.isVisible == true ? (notch().map { wingWidth($0.height) } ?? 0) : 0
        if !force && width == announced { return }
        announced = width
        DistributedNotificationCenter.default().postNotificationName(LUMP_WING, object: nil, userInfo: ["width": Double(width)], deliverImmediately: true)
    }

    func slotChanged(_ info: [AnyHashable: Any]?) {
        let v = ["x", "y", "w", "h"].compactMap { (info?[$0] as? NSNumber)?.doubleValue }
        let rect = v.count == 4 ? NSRect(x: v[0], y: v[1], width: v[2], height: v[3]) : nil
        guard rect != dockRect else { return }
        dockRect = rect
        if placed { place() }
    }

    /// His wing beside the notch: his slot (as tall as the menu bar, less a point top and bottom) plus a little room.
    func wingWidth(_ h: CGFloat) -> CGFloat { ((h - 2) * sprites.aspect + 8).rounded() }

    func showWing(_ n: (screen: NSScreen, height: CGFloat, right: CGFloat)) {
        let f = n.screen.frame, h = n.height
        let docked = dockRect != nil
        let overlap: CGFloat = docked ? 0 : 12, w = wingWidth(h)
        let frame = dockRect ?? NSRect(x: n.right - overlap, y: f.maxY - h, width: w + overlap, height: h)
        if wing == nil {
            let p = NSPanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = false
            p.level = .statusBar
            p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
            p.isReleasedWhenClosed = false
            let v = WingView(frame: NSRect(origin: .zero, size: frame.size))
            v.autoresizingMask = [.width, .height]
            v.onRightClick = { [weak self] e in self?.popMenu(e, in: v) }
            v.onClick = { [weak self] in self?.openRelevant() }
            v.toolTip = "Click to open what Lump's showing you. Right-click for options."
            p.contentView = v
            wing = p; wingView = v
        }
        wingView!.overlap = overlap
        wingView!.docked = docked
        // In the island's slot he sits just above the island's window (main menu + 2), so it can't cover him.
        wing!.level = docked ? NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 3) : .statusBar
        wing!.setFrame(frame, display: true)
        wing!.orderFrontRegardless()
    }
    func hideWing() { wing?.orderOut(nil) }

    func showItem() {
        if item == nil {
            let side = NSStatusBar.system.thickness - 2
            let it = NSStatusBar.system.statusItem(withLength: (side * sprites.aspect + 6).rounded())
            it.button?.imageScaling = .scaleProportionallyUpOrDown
            it.button?.target = self
            it.button?.action = #selector(itemClicked)
            it.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
            it.button?.toolTip = "Click to open what Lump's showing you. Right-click for options."
            item = it
        }
        item?.isVisible = true
    }
    func hideItem() { if let it = item { NSStatusBar.system.removeStatusItem(it); item = nil } }
    @objc func itemClicked() {
        let e = NSApp.currentEvent
        if e?.type == .rightMouseUp || e?.modifierFlags.contains(.control) == true { itemMenu() } else { openRelevant() }
    }
    /// A status item shows its menu on any click once one is set, so set it only while showing it.
    func itemMenu() { guard let it = item else { return }; it.menu = theMenu; it.button?.performClick(nil); it.menu = nil }

    // MARK: click to open

    /// Ask the bridge what's most relevant and open it in the right place.
    func openRelevant() {
        var req = URLRequest(url: OPEN_URL); req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { data, _, _ in
            let j = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async { self.open(url: (j?["url"] as? String).flatMap(URL.init(string:)), account: j?["account"] as? String) }
        }.resume()
    }

    func open(url: URL?, account: String?) {
        if let url = url, url.scheme != "claude" { NSWorkspace.shared.open(url); return }     // Slack, a meeting link
        // a claude:// link has to reach the right copy of the app (personal and work share one bundle id)
        let copy = claudeCopy(account ?? "personal") ?? claudeCopy("personal")
        if let url = url {
            if let c = copy, sendURL(url, to: c.processIdentifier) { bringForward(c) } else { NSWorkspace.shared.open(url) }
        } else if let c = copy {
            bringForward(c)
        } else if let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: CLAUDE_ID) {
            NSWorkspace.shared.openApplication(at: app, configuration: NSWorkspace.OpenConfiguration())
        }
    }

    func bringForward(_ app: NSRunningApplication) {
        NSApp.activate(ignoringOtherApps: true)          // a click on him doesn't make us active, and macOS only lets the active app hand focus on
        app.activate(options: [.activateIgnoringOtherApps])
    }

    /// The running copy of the Claude app for an account: Claude Switcher starts extra accounts with
    /// --user-data-dir=~/Library/Application Support/Claude-<Name>; personal has none.
    func claudeCopy(_ account: String) -> NSRunningApplication? {
        NSRunningApplication.runningApplications(withBundleIdentifier: CLAUDE_ID).first { accountOf($0.processIdentifier) == account }
    }
    private var accounts: [pid_t: String] = [:]
    func accountOf(_ pid: pid_t) -> String {
        if let a = accounts[pid] { return a }
        let ps = Process(), pipe = Pipe()
        ps.executableURL = URL(fileURLWithPath: "/bin/ps"); ps.arguments = ["-o", "command=", "-p", String(pid)]
        ps.standardOutput = pipe
        guard (try? ps.run()) != nil else { return "personal" }
        ps.waitUntilExit()
        let cmd = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        var a = "personal"
        if let r = cmd.range(of: #"--user-data-dir=(.+?)(?= --|$)"#, options: .regularExpression) {
            let dir = (String(cmd[r].dropFirst("--user-data-dir=".count)).trimmingCharacters(in: .whitespacesAndNewlines) as NSString).lastPathComponent
            if dir.hasPrefix("Claude-") { a = String(dir.dropFirst("Claude-".count)).lowercased() }
        }
        accounts[pid] = a
        return a
    }

    /// Hand a link to one particular process, the way `open` would but without letting macOS pick the copy:
    /// an Apple "get URL" event. macOS asks once whether Lump may control Claude.
    func sendURL(_ url: URL, to pid: pid_t) -> Bool {
        let GURL: UInt32 = 0x4755_524C                   // 'GURL': both the event class and id
        let ev = NSAppleEventDescriptor(eventClass: GURL, eventID: GURL,
                                        targetDescriptor: NSAppleEventDescriptor(processIdentifier: pid),
                                        returnID: AEReturnID(kAutoGenerateReturnID), transactionID: AETransactionID(kAnyTransactionID))
        ev.setParam(NSAppleEventDescriptor(string: url.absoluteString), forKeyword: 0x2D2D_2D2D)   // '----': the direct object
        do { _ = try ev.sendEvent(options: [.noReply], timeout: 5); return true }
        catch { NSLog("lumpbar: couldn't hand %@ to Claude (pid %d): %@", url.absoluteString, pid, "\(error)"); return false }
    }

    // MARK: menu (rebuilt each time it opens, so the monitor list is current)

    func menuNeedsUpdate(_ m: NSMenu) {
        m.removeAllItems()
        func add(_ title: String, _ action: Selector?, on: Bool = false, to menu: NSMenu = m, rep: Any? = nil) {
            let it = NSMenuItem(title: title, action: action, keyEquivalent: "")
            it.target = self; it.state = on ? .on : .off; it.representedObject = rep; it.isEnabled = action != nil
            menu.addItem(it)
        }
        add("Only When I'm Away From My Desk", #selector(setShowAway), on: !showAlways)
        add("All the Time", #selector(setShowAlways), on: showAlways)
        m.addItem(.separator())

        let desk = NSMenu()
        add("Any External Display", #selector(pickDesk(_:)), on: deskDisplay == nil, to: desk)
        var names = externalDisplays()
        if let d = deskDisplay, !names.contains(d) { names.insert(d, at: 0) }   // remembered, just not plugged in now
        if !names.isEmpty { desk.addItem(.separator()) }
        for n in names { add(n, #selector(pickDesk(_:)), on: n == deskDisplay, to: desk, rep: n) }
        let deskItem = NSMenuItem(title: "My Desk Monitor", action: nil, keyEquivalent: "")
        deskItem.submenu = desk
        m.addItem(deskItem)
        add(atDeskHint(), nil)
        m.addItem(.separator())

        add("Always in the Menu Bar", #selector(toggleMenuBar), on: preferMenuBar)
        m.addItem(.separator())
        m.addItem(NSMenuItem(title: "Quit Lump", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }
    func atDeskHint() -> String {
        if showAlways { return "Showing all the time" }
        let mon = deskDisplayConnected(), dev = Date().timeIntervalSince(deviceSeen) < DEVICE_GRACE
        if mon && dev { return Date() < peekUntil ? "You're at your desk, so he hides when this closes" : "At your desk" }
        return !mon ? "Away: desk monitor not plugged in" : "Away: desk Lump is offline"
    }
    func popMenu(_ e: NSEvent, in v: NSView) { NSMenu.popUpContextMenu(theMenu, with: e, for: v) }
    @objc func toggleMenuBar() { preferMenuBar.toggle(); place() }
    @objc func setShowAway() { showAlways = false; place() }
    @objc func setShowAlways() { showAlways = true; place() }
    @objc func pickDesk(_ sender: NSMenuItem) { deskDisplay = sender.representedObject as? String; place() }

    // MARK: mood

    func poll() {
        var req = URLRequest(url: STATUS_URL); req.timeoutInterval = 0.8
        URLSession.shared.dataTask(with: req) { data, _, _ in
            let j = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                defer {
                    // Matchday quit (or crashed) without saying so: take his own wing back.
                    if self.dockRect != nil, NSRunningApplication.runningApplications(withBundleIdentifier: MATCHDAY_ID).isEmpty {
                        self.dockRect = nil; self.place()
                    }
                    if !self.placed || self.computeAtDesk() != self.atDesk { self.placed = true; self.place() }
                    if self.menuWhenPlaced { self.menuWhenPlaced = false; self.showMenu() }
                }
                guard let j = j else { self.online = false; return }
                self.online = true
                if let m = j["mood"] as? String, self.sprites.manifest.moods[m] != nil, m != self.mood {
                    self.mood = m; if self.playing == nil { self.tick = 0 }
                }
                if let e = j["event"] as? [String: Any], let n = e["n"] as? Int, n != self.lastEventN {
                    let fresh = (e["age_s"] as? Double ?? 99) < 5
                    if self.lastEventN >= 0, fresh, let name = e["name"] as? String, self.sprites.manifest.events?[name] != nil {
                        self.playing = name; self.tick = 0
                    }
                    self.lastEventN = n
                }
                if let c = j["colour"] as? String, c.count == 6 { self.colour = c.uppercased() }
                if j["device"] as? Bool == true { self.deviceSeen = Date() }
            }
        }.resume()
    }

    func step() {
        var cur = playing ?? mood
        var ticks = sprites.ticks(cur)
        if playing != nil, tick >= ticks.count { playing = nil; tick = 0; cur = mood; ticks = sprites.ticks(cur) }   // played once: back to his mood
        guard !ticks.isEmpty else { return }
        let idx = ticks[tick % ticks.count]; tick += 1
        let dark: Bool
        if let b = item?.button { dark = b.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua } else { dark = true }
        let ink = (wing?.isVisible ?? false) || dark ? "glow" : "paper"   // the wing is always black
        let dim: CGFloat = !online ? 0.4 : mood == "night" ? 0.55 : 1     // asleep at night, faded when the bridge is down
        let key = "\(cur)-\(wing?.isVisible == true)-\(ink)-\(colour)-\(idx)-\(dim)"
        if key == shownKey { return }
        shownKey = key
        let size = wing?.isVisible == true ? "wing" : "item"
        let frames = sprites.frames(cur, size: size, ink: ink, colour: colour)
        guard idx < frames.count else { return }
        if let v = wingView, wing?.isVisible == true { v.dim = dim; v.image = frames[idx] }
        if let b = item?.button {
            let side = NSStatusBar.system.thickness - 2
            let img = NSImage(cgImage: frames[idx], size: NSSize(width: side * sprites.aspect, height: side))
            b.image = img; b.alphaValue = dim
        }
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
