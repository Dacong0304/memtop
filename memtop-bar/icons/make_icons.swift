// MemTopBar 图标生成器 —— 两个方案，输出 1024 PNG + 对照预览
// 用法：swift make_icons.swift <输出目录>

import AppKit
import Foundation

let S: CGFloat = 1024
let outDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "."

// MARK: - 画布

func makeCanvas() -> (NSBitmapImageRep, NSGraphicsContext) {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil,
                               pixelsWide: Int(S), pixelsHigh: Int(S),
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                               isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!
    return (rep, ctx)
}

func save(_ rep: NSBitmapImageRep, _ name: String) {
    let data = rep.representation(using: .png, properties: [:])!
    try! data.write(to: URL(fileURLWithPath: (outDir as NSString).appendingPathComponent(name)))
}

/// 苹果风格的连续圆角（超椭圆），比普通 rounded rect 更接近 Big Sur 图标
func squircle(_ rect: NSRect, n: CGFloat = 5) -> NSBezierPath {
    let p = NSBezierPath()
    let a = rect.width / 2, b = rect.height / 2
    let cx = rect.midX, cy = rect.midY
    let steps = 720
    for i in 0...steps {
        let t = CGFloat(i) / CGFloat(steps) * 2 * .pi
        let ct = cos(t), st = sin(t)
        let x = cx + a * copysign(pow(abs(ct), 2 / n), ct)
        let y = cy + b * copysign(pow(abs(st), 2 / n), st)
        if i == 0 { p.move(to: NSPoint(x: x, y: y)) } else { p.line(to: NSPoint(x: x, y: y)) }
    }
    p.close()
    return p
}

func gradient(_ colors: [NSColor], _ angle: CGFloat) -> NSGradient {
    NSGradient(colors: colors)!
}

func drawBase(_ path: NSBezierPath, top: NSColor, bottom: NSColor, glow: NSColor, glowCenter: NSPoint) {
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.45)
    shadow.shadowBlurRadius = 34
    shadow.shadowOffset = NSSize(width: 0, height: -16)
    shadow.set()
    NSColor(calibratedWhite: 0.04, alpha: 1).setFill()
    path.fill()
    NSGraphicsContext.restoreGraphicsState()

    NSGraphicsContext.saveGraphicsState()
    path.addClip()
    gradient([top, bottom], -90).draw(in: path, angle: -90)

    // 顶部高光
    let sheen = NSBezierPath(rect: NSRect(x: 0, y: S * 0.55, width: S, height: S * 0.45))
    gradient([NSColor.white.withAlphaComponent(0.10), NSColor.white.withAlphaComponent(0.0)], 90)
        .draw(in: sheen, angle: -90)

    // 内描边
    NSGraphicsContext.restoreGraphicsState()
    path.lineWidth = 3
    NSColor.white.withAlphaComponent(0.10).setStroke()
    path.stroke()
}

// MARK: - 方案 A：用量环 + 内存颗粒

func ringIcon() -> NSBitmapImageRep {
    let (rep, ctx) = makeCanvas()
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx
    ctx.imageInterpolation = .high

    let body = squircle(NSRect(x: 100, y: 100, width: 824, height: 824))
    drawBase(body,
             top: NSColor(srgbRed: 0.075, green: 0.106, blue: 0.157, alpha: 1),
             bottom: NSColor(srgbRed: 0.031, green: 0.043, blue: 0.063, alpha: 1),
             glow: NSColor(srgbRed: 0.34, green: 0.84, blue: 0.78, alpha: 0.16),
             glowCenter: NSPoint(x: 340, y: 760))

    let center = NSPoint(x: 512, y: 512)
    let radius: CGFloat = 246
    let width: CGFloat = 62

    // 轨道
    let track = NSBezierPath()
    track.appendArc(withCenter: center, radius: radius, startAngle: 0, endAngle: 360)
    track.lineWidth = width
    NSColor.white.withAlphaComponent(0.07).setStroke()
    track.stroke()

    // 用量弧：从 -215° 扫到 +35°（约 250°），整段填充线性渐变
    let startA: CGFloat = -215, sweep: CGFloat = 250
    let outer = radius + width / 2
    let inner = radius - width / 2
    let arc = NSBezierPath()
    arc.appendArc(withCenter: center, radius: outer, startAngle: startA, endAngle: startA + sweep)
    arc.appendArc(withCenter: center, radius: inner, startAngle: startA + sweep, endAngle: startA, clockwise: true)
    arc.close()

    let c1 = NSColor(srgbRed: 0.37, green: 0.92, blue: 0.84, alpha: 1)
    let c2 = NSColor(srgbRed: 0.42, green: 0.66, blue: 1.0, alpha: 1)
    gradient([c1, c2], 0).draw(in: arc, angle: 0)

    // 两端的圆头
    for (angle, color) in [(startA, c1), (startA + sweep, c2)] {
        let rad = angle * .pi / 180
        let c = NSPoint(x: center.x + cos(rad) * radius, y: center.y + sin(rad) * radius)
        color.setFill()
        NSBezierPath(ovalIn: NSRect(x: c.x - width / 2, y: c.y - width / 2,
                                    width: width, height: width)).fill()
    }

    // 中心：内存颗粒
    let chip = NSRect(x: 512 - 104, y: 512 - 104, width: 208, height: 208)
    let chipPath = NSBezierPath(roundedRect: chip, xRadius: 46, yRadius: 46)
    NSColor(srgbRed: 0.055, green: 0.075, blue: 0.106, alpha: 1).setFill()
    chipPath.fill()
    chipPath.lineWidth = 11
    NSColor.white.withAlphaComponent(0.86).setStroke()
    chipPath.stroke()

    // 颗粒引脚
    let pinColor = NSColor.white.withAlphaComponent(0.78)
    pinColor.setStroke()
    for side in 0..<4 {
        for k in [-1, 1] as [CGFloat] {
            let p = NSBezierPath()
            p.lineWidth = 11
            p.lineCapStyle = .round
            let off = k * 52
            switch side {
            case 0: // 上
                p.move(to: NSPoint(x: 512 + off, y: chip.maxY))
                p.line(to: NSPoint(x: 512 + off, y: chip.maxY + 26))
            case 1: // 下
                p.move(to: NSPoint(x: 512 + off, y: chip.minY))
                p.line(to: NSPoint(x: 512 + off, y: chip.minY - 26))
            case 2: // 左
                p.move(to: NSPoint(x: chip.minX, y: 512 + off))
                p.line(to: NSPoint(x: chip.minX - 26, y: 512 + off))
            default: // 右
                p.move(to: NSPoint(x: chip.maxX, y: 512 + off))
                p.line(to: NSPoint(x: chip.maxX + 26, y: 512 + off))
            }
            p.stroke()
        }
    }

    // 中心小色块
    let core = NSBezierPath(roundedRect: NSRect(x: 512 - 30, y: 512 - 30, width: 60, height: 60),
                            xRadius: 14, yRadius: 14)
    NSColor(srgbRed: 0.37, green: 0.88, blue: 0.82, alpha: 1).setFill()
    core.fill()

    NSGraphicsContext.restoreGraphicsState()
    return rep
}

// MARK: - 方案 B：容量条

func barsIcon() -> NSBitmapImageRep {
    let (rep, ctx) = makeCanvas()
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx

    let body = squircle(NSRect(x: 100, y: 100, width: 824, height: 824))
    drawBase(body,
             top: NSColor(srgbRed: 0.063, green: 0.094, blue: 0.14, alpha: 1),
             bottom: NSColor(srgbRed: 0.027, green: 0.039, blue: 0.059, alpha: 1),
             glow: NSColor(srgbRed: 0.42, green: 0.66, blue: 1.0, alpha: 0.26),
             glowCenter: NSPoint(x: 700, y: 300))

    let trackW: CGFloat = 500
    let left: CGFloat = 512 - trackW / 2
    let barH: CGFloat = 104
    let gap: CGFloat = 38
    let totalH = barH * 3 + gap * 2
    let fills: [CGFloat] = [1.0, 0.70, 0.42]
    let cols: [(NSColor, NSColor)] = [
        (NSColor(srgbRed: 0.37, green: 0.92, blue: 0.84, alpha: 1),
         NSColor(srgbRed: 0.36, green: 0.80, blue: 0.96, alpha: 1)),
        (NSColor(srgbRed: 0.36, green: 0.80, blue: 0.96, alpha: 1),
         NSColor(srgbRed: 0.43, green: 0.66, blue: 1.0, alpha: 1)),
        (NSColor(srgbRed: 0.48, green: 0.62, blue: 0.98, alpha: 1),
         NSColor(srgbRed: 0.62, green: 0.55, blue: 0.98, alpha: 1)),
    ]

    // 整体轻微旋转，避免像普通列表
    let tf = NSAffineTransform()
    tf.translateX(by: 512, yBy: 512)
    tf.rotate(byDegrees: -7)
    tf.translateX(by: -512, yBy: -512)
    tf.concat()

    var y = 512 + totalH / 2 - barH
    for i in 0..<3 {
        let trackRect = NSRect(x: left, y: y, width: trackW, height: barH)
        let track = NSBezierPath(roundedRect: trackRect, xRadius: barH / 2, yRadius: barH / 2)
        NSColor.white.withAlphaComponent(0.06).setFill()
        track.fill()

        let w = trackW * fills[i]
        let fillRect = NSRect(x: left, y: y, width: w, height: barH)
        let fill = NSBezierPath(roundedRect: fillRect, xRadius: barH / 2, yRadius: barH / 2)

        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowColor = cols[i].0.withAlphaComponent(0.5)
        shadow.shadowBlurRadius = 30
        shadow.shadowOffset = NSSize(width: 0, height: -6)
        shadow.set()
        gradient([cols[i].0, cols[i].1], 0).draw(in: fill, angle: 0)
        NSGraphicsContext.restoreGraphicsState()

        // 顶部高光
        NSGraphicsContext.saveGraphicsState()
        fill.addClip()
        gradient([NSColor.white.withAlphaComponent(0.28), NSColor.white.withAlphaComponent(0)],
                 90).draw(in: NSRect(x: fillRect.minX, y: fillRect.midY,
                                     width: w, height: barH / 2), angle: -90)
        NSGraphicsContext.restoreGraphicsState()

        y -= (barH + gap)
    }

    NSGraphicsContext.restoreGraphicsState()
    return rep
}

// MARK: - 对照预览（浅色 / 深色底 + 小尺寸）

func preview() -> NSBitmapImageRep {
    let w: CGFloat = 1180, h: CGFloat = 780
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(w), pixelsHigh: Int(h),
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                               isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx

    NSColor(srgbRed: 0.945, green: 0.949, blue: 0.96, alpha: 1).setFill()
    NSRect(x: 0, y: 0, width: w / 2, height: h).fill()
    NSColor(srgbRed: 0.11, green: 0.11, blue: 0.12, alpha: 1).setFill()
    NSRect(x: w / 2, y: 0, width: w / 2, height: h).fill()

    let icons = [("A · Gauge", ringIcon()), ("B · Bars", barsIcon())]
    let sizes: [CGFloat] = [240, 112, 64]
    let rowCenters: [CGFloat] = [h - 220, 200]

    for (row, item) in icons.enumerated() {
        let img = NSImage(size: NSSize(width: 1024, height: 1024))
        img.addRepresentation(item.1)
        let cy = rowCenters[row]
        for half in 0..<2 {
            let baseX = CGFloat(half) * (w / 2)
            let color = half == 0
                ? NSColor(srgbRed: 0.25, green: 0.27, blue: 0.32, alpha: 1)
                : NSColor.white.withAlphaComponent(0.75)
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 24, weight: .medium),
                .foregroundColor: color,
            ]
            (item.0 as NSString).draw(at: NSPoint(x: baseX + 44, y: cy + 142), withAttributes: attrs)

            var x = baseX + 44
            for (i, size) in sizes.enumerated() {
                let rect = NSRect(x: x, y: cy - size / 2 + 20, width: size, height: size)
                img.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1)
                x += size + (i == 0 ? 46 : 36)
            }
        }
    }

    NSGraphicsContext.restoreGraphicsState()
    return rep
}

save(ringIcon(), "icon-a.png")
save(barsIcon(), "icon-b.png")
save(preview(), "preview.png")
print("written to \(outDir)")
