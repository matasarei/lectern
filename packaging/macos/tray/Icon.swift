// Lectern.app's icon, drawn in code: an open book with an illuminated "L" on its first page.
// build_app.sh runs the built app with --draw-icon to write the .iconset that iconutil turns
// into Lectern.icns. No artwork is kept in the repository, and the letter is set in a font
// this Mac already has. (The menu bar uses a system symbol, which Apple doesn't allow in an
// app's icon; hence a book of our own.)
import AppKit

enum Icon {
    // What an .iconset holds: the size in points, and whether it's the @2x file.
    private static let files: [(points: Int, scale: Int)] = [
        (16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2),
    ]

    static func writeSet(into folder: String) -> Bool {
        do {
            try FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
            for file in files {
                let name = "icon_\(file.points)x\(file.points)\(file.scale == 2 ? "@2x" : "").png"
                guard let png = png(pixels: file.points * file.scale) else { return false }
                try png.write(to: URL(fileURLWithPath: folder).appendingPathComponent(name))
            }
            return true
        } catch {
            return false
        }
    }

    private static func png(pixels: Int) -> Data? {
        guard
            let bitmap = NSBitmapImageRep(
                bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                bytesPerRow: 0, bitsPerPixel: 0),
            let context = NSGraphicsContext(bitmapImageRep: bitmap)
        else { return nil }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        let scale = CGFloat(pixels) / 1024
        context.cgContext.scaleBy(x: scale, y: scale)
        draw()
        NSGraphicsContext.restoreGraphicsState()
        return bitmap.representation(using: .png, properties: [:])
    }

    private static func color(_ hex: Int, alpha: CGFloat = 1) -> NSColor {
        NSColor(
            srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
    }

    // Everything below is on a 1024-point canvas, y upwards, the book's spine at x = 512.

    private static func draw() {
        // The plate, in the size and corner macOS gives its own icons, in the app's green.
        let plate = NSBezierPath(
            roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824), xRadius: 185, yRadius: 185)
        NSGraphicsContext.saveGraphicsState()
        let lift = NSShadow()
        lift.shadowOffset = NSSize(width: 0, height: -12)
        lift.shadowBlurRadius = 24
        lift.shadowColor = NSColor.black.withAlphaComponent(0.3)
        lift.set()
        color(0x2C6A58).setFill()
        plate.fill()
        NSGraphicsContext.restoreGraphicsState()
        NSGradient(starting: color(0x3A8570), ending: color(0x1F4F41))?.draw(in: plate, angle: -90)

        // The cover, showing under the pages.
        color(0x3B2416).setFill()
        page(spineTop: 672, outerTop: 690, outerBottom: 312, spineBottom: 294, outerX: 186, mirrored: false).fill()
        page(spineTop: 672, outerTop: 690, outerBottom: 312, spineBottom: 294, outerX: 186, mirrored: true).fill()

        // The two pages, lighter towards their outer edges.
        let left = page(spineTop: 690, outerTop: 706, outerBottom: 344, spineBottom: 326, outerX: 208, mirrored: false)
        let right = page(spineTop: 690, outerTop: 706, outerBottom: 344, spineBottom: 326, outerX: 208, mirrored: true)
        NSGradient(starting: color(0xFBF5E6), ending: color(0xE6D9BC))?.draw(in: left, angle: 0)
        NSGradient(starting: color(0xE6D9BC), ending: color(0xFBF5E6))?.draw(in: right, angle: 0)
        // The fold between them.
        NSGradient(colors: [color(0x3B2416, alpha: 0), color(0x3B2416, alpha: 0.35), color(0x3B2416, alpha: 0)])?
            .draw(in: NSRect(x: 488, y: 326, width: 48, height: 372), angle: 0)

        // Lines of writing on the second page, the first one indented.
        color(0x8C7B5C, alpha: 0.85).setStroke()
        for row in 0..<6 {
            let y = CGFloat(630 - row * 48)
            let line = NSBezierPath()
            line.lineWidth = 11
            line.lineCapStyle = .round
            line.move(to: NSPoint(x: row == 0 ? 610 : 566, y: y - 4))
            line.curve(
                to: NSPoint(x: row == 5 ? 700 : 772, y: y), controlPoint1: NSPoint(x: 640, y: y + 8),
                controlPoint2: NSPoint(x: 720, y: y + 6))
            line.stroke()
        }

        // The first page: one large initial, as a manuscript opens.
        let initial = NSAttributedString(
            string: "L",
            attributes: [.font: initialFont(size: 330), .foregroundColor: color(0x8A1C1C)])
        let size = initial.size()
        initial.draw(at: NSPoint(x: 360 - size.width / 2, y: 512 - size.height / 2))
    }

    // One page: from the spine's top over the page's swell to its outer edge, down, and back.
    private static func page(
        spineTop: CGFloat, outerTop: CGFloat, outerBottom: CGFloat, spineBottom: CGFloat, outerX: CGFloat,
        mirrored: Bool
    ) -> NSBezierPath {
        func x(_ value: CGFloat) -> CGFloat { mirrored ? 1024 - value : value }
        let path = NSBezierPath()
        path.move(to: NSPoint(x: x(512), y: spineTop))
        path.curve(
            to: NSPoint(x: x(outerX), y: outerTop), controlPoint1: NSPoint(x: x(440), y: spineTop + 46),
            controlPoint2: NSPoint(x: x(300), y: outerTop + 30))
        path.line(to: NSPoint(x: x(outerX), y: outerBottom))
        path.curve(
            to: NSPoint(x: x(512), y: spineBottom), controlPoint1: NSPoint(x: x(300), y: outerBottom + 30),
            controlPoint2: NSPoint(x: x(440), y: spineBottom + 46))
        path.close()
        return path
    }

    // A hand of the manuscript kind where macOS has one, else a serif.
    private static func initialFont(size: CGFloat) -> NSFont {
        for name in ["Luminari", "Trattatello", "Baskerville-Bold", "TimesNewRomanPS-BoldMT"] {
            if let font = NSFont(name: name, size: size) { return font }
        }
        return NSFont.boldSystemFont(ofSize: size)
    }
}
