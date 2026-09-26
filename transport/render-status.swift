import Foundation
import CoreGraphics
import CoreText
import ImageIO
import UniformTypeIdentifiers

let out = CommandLine.arguments[1]
var limits: [String: Any] = [:]
if CommandLine.arguments.count > 2,
   let data = try? Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2])) {
    limits = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
}
try FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)
let states: [(String, String, [CGFloat])] = [
    ("working", "WORKING", [0.24, 0.72, 1]), ("waiting", "NEEDS YOU", [1, 0.72, 0.24]),
    ("done", "DONE", [0.30, 0.90, 0.65]), ("idle", "IDLE", [0.63, 0.70, 0.81])
]
let space = CGColorSpaceCreateDeviceRGB()
func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat) -> CGColor {
    CGColor(colorSpace: space, components: [r, g, b, 1])!
}
let white = rgb(1, 1, 1)
let muted = rgb(0.73, 0.79, 0.87)
for (state, label, components) in states {
    let ctx = CGContext(data: nil, width: 160, height: 128, bitsPerComponent: 8,
        bytesPerRow: 640, space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
    let accent = CGColor(colorSpace: space, components: components + [1])!
    func rounded(_ rect: CGRect, radius: CGFloat, color: CGColor) {
        ctx.setFillColor(color)
        ctx.addPath(CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil))
        ctx.fillPath()
    }
    func text(_ value: String, x: CGFloat, y: CGFloat, size: CGFloat,
              color: CGColor, right: Bool = false, bold: Bool = true, centered: Bool = false) {
        let attrs: [NSAttributedString.Key: Any] = [
            NSAttributedString.Key(kCTFontAttributeName as String): CTFontCreateWithName((bold ? "Menlo-Bold" : "Menlo") as CFString, size, nil),
            NSAttributedString.Key(kCTForegroundColorAttributeName as String): color]
        let line = CTLineCreateWithAttributedString(NSAttributedString(string: value, attributes: attrs))
        let width = CTLineGetTypographicBounds(line, nil, nil, nil)
        ctx.setShouldSmoothFonts(false)
        let bounds = CTLineGetBoundsWithOptions(line, .useGlyphPathBounds)
        let baseline = centered ? (y - bounds.midY).rounded() : y
        ctx.textPosition = CGPoint(x: right ? x - width : x, y: baseline)
        CTLineDraw(line, ctx)
    }
    ctx.setFillColor(rgb(0.025, 0.04, 0.075))
    ctx.fill(CGRect(x: 0, y: 0, width: 160, height: 128))
    text("CODEX", x: 8, y: 116, size: 10, color: white)
    let fresh = Date().timeIntervalSince1970 - (limits["fetched_at"] as? Double ?? 0) < 300
    text(fresh ? "LIMITS" : "OLD DATA", x: 152, y: 116, size: 9, color: fresh ? muted : rgb(1, 0.72, 0.24), right: true)

    // Integer-aligned bitmap glyphs remain legible on the native LCD.
    rounded(CGRect(x: 5, y: 87, width: 150, height: 26), radius: 4, color: rgb(0.08, 0.13, 0.20))
    let glyphs: [String: [String]] = [
        "working": ["10000", "01000", "00100", "01000", "10000", "00000", "00111"],
        "waiting": ["00100", "00100", "00100", "00100", "00000", "00100", "00100"],
        "done":    ["00001", "00001", "00010", "00010", "10100", "01100", "01000"],
        "idle":    ["11011", "11011", "11011", "11011", "11011", "11011", "11011"]
    ]
    ctx.setShouldAntialias(false)
    ctx.setFillColor(accent)
    for (row, line) in glyphs[state]!.enumerated() {
        for (column, pixel) in line.enumerated() where pixel == "1" {
            ctx.fill(CGRect(x: 12 + column * 2, y: 105 - row * 2, width: 2, height: 2))
        }
    }
    ctx.setShouldAntialias(true)
    text(label, x: 32, y: 100, size: 18, color: accent, centered: true)

    let windows = limits["windows"] as? [[String: Any]] ?? [[:], [:]]
    for index in 0..<2 {
        let window = index < windows.count ? windows[index] : [:]
        let bottom: CGFloat = index == 0 ? 44 : 1
        rounded(CGRect(x: 5, y: bottom, width: 150, height: 40), radius: 4, color: rgb(0.055, 0.078, 0.12))
        let remaining = (window["remaining"] as? Double).map { max(0, min(100, $0)) }
        let percent = remaining.map { String(format: "%.0f%%", $0) } ?? "--%"
        let quotaColor = (remaining ?? 100) <= 10 ? rgb(1, 0.36, 0.31) : (index == 0 ? rgb(0.25, 0.76, 1) : rgb(0.68, 0.53, 1))
        let name = window["label"] as? String ?? (index == 0 ? "5H" : "WEEK")
        text(name, x: 12, y: bottom + 30, size: 12, color: muted, centered: true)
        text(percent, x: 150, y: bottom + 30, size: 18, color: white, right: true, centered: true)
        rounded(CGRect(x: 12, y: bottom + 14, width: 138, height: 5), radius: 2, color: rgb(0.13, 0.18, 0.25))
        if let remaining = remaining, remaining > 0 {
            rounded(CGRect(x: 12, y: bottom + 14, width: (138 * remaining / 100).rounded(), height: 5), radius: 2, color: quotaColor)
        }
        var reset = "RESET --"
        if let stamp = window["resets_at"] as? Double {
            let formatter = DateFormatter()
            formatter.dateFormat = index == 0 ? "HH:mm" : "dd.MM HH:mm"
            reset = "RESET " + formatter.string(from: Date(timeIntervalSince1970: stamp))
        }
        text(reset, x: 12, y: bottom + 6, size: 8, color: muted, bold: false, centered: true)
    }
    let url = URL(fileURLWithPath: out).appendingPathComponent(state + ".jpg")
    let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(dest, ctx.makeImage()!, [kCGImageDestinationLossyCompressionQuality: 1.0] as CFDictionary)
    guard CGImageDestinationFinalize(dest) else { fatalError("JPEG encoding failed") }
}
