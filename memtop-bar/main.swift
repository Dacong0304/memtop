// MemTopBar — macOS 菜单栏内存速查（数据来自 memtop --json）
//
// 构建：./build.sh
// 调试：MemTopBar.app/Contents/MacOS/MemTopBar --snapshot /tmp/panel.png

import AppKit
import Foundation

struct MTFailure: Error {
    let message: String
}

// MARK: - 数据模型

struct MTMember: Decodable {
    let pid: Int
    let name: String
    let footprint: Int
    let compressed: Int
    let user: String
    let protected: Bool
    let risk: String
    let reason: String
    let elapsed: String
    let bundle: String?
}

struct MTGroup: Decodable {
    let name: String
    let app: Bool
    let bundle: String?
    let footprint: Int
    let compressed: Int
    let risk: String
    let protected: Bool
    let killable: Int
    let mine: Bool
    let members: [MTMember]
}

struct MTMemory: Decodable {
    let total: Int
    let used: Int
    let app: Int
    let wired: Int
    let compressed: Int
    let cached: Int
    let free: Int
    let swap_total: Int
    let swap_used: Int
    let pressure: Int
    let pressure_label: String
    let reclaimable: Int
    let compressed_killable: Int?
}

struct MTSnapshot: Decodable {
    let processes: Int
    let memory: MTMemory
    let groups: [MTGroup]
}

// MARK: - 调色板

enum MT {
    static let bg = NSColor(srgbRed: 0.045, green: 0.058, blue: 0.078, alpha: 1)
    static let panel = NSColor(srgbRed: 0.075, green: 0.094, blue: 0.125, alpha: 1)
    static let line = NSColor.white.withAlphaComponent(0.075)
    static let text = NSColor(srgbRed: 0.913, green: 0.933, blue: 0.965, alpha: 1)
    static let dim = NSColor(srgbRed: 0.565, green: 0.612, blue: 0.694, alpha: 1)
    static let faint = NSColor(srgbRed: 0.36, green: 0.40, blue: 0.47, alpha: 1)
    static let teal = NSColor(srgbRed: 0.341, green: 0.839, blue: 0.784, alpha: 1)
    static let blue = NSColor(srgbRed: 0.427, green: 0.659, blue: 1.0, alpha: 1)
    static let violet = NSColor(srgbRed: 0.690, green: 0.545, blue: 1.0, alpha: 1)
    static let amber = NSColor(srgbRed: 0.949, green: 0.706, blue: 0.361, alpha: 1)
    static let red = NSColor(srgbRed: 1.0, green: 0.435, blue: 0.435, alpha: 1)
    static let green = NSColor(srgbRed: 0.373, green: 0.863, blue: 0.627, alpha: 1)

    static func size(_ bytes: Int) -> String {
        let n = Double(max(0, bytes))
        if n >= 1024 * 1024 * 1024 * 1024 { return String(format: "%.2f TB", n / pow(1024, 4)) }
        if n >= 1024 * 1024 * 1024 {
            let v = n / pow(1024, 3)
            return String(format: v >= 100 ? "%.0f GB" : "%.1f GB", v)
        }
        if n >= 1024 * 1024 {
            let v = n / pow(1024, 2)
            return String(format: v >= 100 ? "%.0f MB" : "%.0f MB", v)
        }
        return String(format: "%.0f KB", n / 1024)
    }

    static func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular,
                      color: NSColor = MT.text, mono: Bool = false) -> NSTextField {
        let f = NSTextField(labelWithString: text)
        f.font = mono
            ? NSFont.monospacedDigitSystemFont(ofSize: size, weight: weight)
            : NSFont.systemFont(ofSize: size, weight: weight)
        f.textColor = color
        f.lineBreakMode = .byTruncatingTail
        f.translatesAutoresizingMaskIntoConstraints = false
        return f
    }
}

// MARK: - 小控件

final class MTBar: NSView {
    var segments: [(Double, NSColor)] = [] {
        didSet { needsDisplay = true }
    }

    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let r = bounds
        let bg = NSBezierPath(roundedRect: r, xRadius: 4, yRadius: 4)
        NSColor.white.withAlphaComponent(0.07).setFill()
        bg.fill()

        NSGraphicsContext.saveGraphicsState()
        bg.addClip()
        var x: CGFloat = 0
        for (frac, color) in segments {
            let w = r.width * CGFloat(max(0, min(1, frac)))
            color.setFill()
            NSRect(x: x, y: 0, width: max(0, w - 0.5), height: r.height).fill()
            x += w
        }
        NSGraphicsContext.restoreGraphicsState()
    }
}

final class MTButton: NSButton {
    private var handler: (() -> Void)?
    private var hover = false { didSet { needsDisplay = true } }

    convenience init(_ title: String, size: CGFloat = 11.5,
                     tint: NSColor = MT.dim, handler: @escaping () -> Void) {
        self.init(frame: .zero)
        self.handler = handler
        self.isBordered = false
        self.bezelStyle = .inline
        self.attributedTitle = NSAttributedString(string: title, attributes: [
            .foregroundColor: tint,
            .font: NSFont.systemFont(ofSize: size, weight: .medium),
        ])
        self.target = self
        self.action = #selector(fire)
        self.translatesAutoresizingMaskIntoConstraints = false
        self.wantsLayer = true
        self.layer?.cornerRadius = 6
        let area = NSTrackingArea(rect: .zero,
                                  options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                  owner: self, userInfo: nil)
        addTrackingArea(area)
    }

    @objc private func fire() { handler?() }

    func setTitle(_ text: String, size: CGFloat = 9.5, tint: NSColor = MT.dim) {
        attributedTitle = NSAttributedString(string: text, attributes: [
            .foregroundColor: tint,
            .font: NSFont.systemFont(ofSize: size, weight: .medium),
        ])
    }

    override func mouseEntered(with event: NSEvent) {
        layer?.backgroundColor = NSColor.white.withAlphaComponent(0.08).cgColor
    }

    override func mouseExited(with event: NSEvent) {
        layer?.backgroundColor = NSColor.clear.cgColor
    }
}

final class MTKillAllButton: NSButton {
    private var handler: (() -> Void)?

    convenience init(_ title: String, width: CGFloat = 44, handler: @escaping () -> Void) {
        self.init(frame: .zero)
        self.handler = handler
        isBordered = false
        bezelStyle = .inline
        wantsLayer = true
        layer?.cornerRadius = 6
        layer?.backgroundColor = MT.red.withAlphaComponent(0.10).cgColor
        layer?.borderWidth = 1
        layer?.borderColor = MT.red.withAlphaComponent(0.26).cgColor
        attributedTitle = NSAttributedString(string: title, attributes: [
            .foregroundColor: MT.red,
            .font: NSFont.systemFont(ofSize: 10.5, weight: .medium),
        ])
        target = self
        action = #selector(fire)
        translatesAutoresizingMaskIntoConstraints = false
        widthAnchor.constraint(equalToConstant: width).isActive = true
        heightAnchor.constraint(equalToConstant: 20).isActive = true
        let area = NSTrackingArea(rect: .zero,
                                  options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                  owner: self, userInfo: nil)
        addTrackingArea(area)
    }

    @objc private func fire() { handler?() }

    override func mouseEntered(with event: NSEvent) {
        layer?.backgroundColor = MT.red.withAlphaComponent(0.20).cgColor
    }

    override func mouseExited(with event: NSEvent) {
        layer?.backgroundColor = MT.red.withAlphaComponent(0.10).cgColor
    }
}

final class MTSelectButton: NSButton {
    private var handler: (() -> Void)?

    convenience init(checked: Bool, active: Bool, handler: @escaping () -> Void) {
        self.init(frame: .zero)
        self.handler = handler
        isBordered = false
        bezelStyle = .inline
        imagePosition = .imageOnly
        setButtonType(.momentaryChange)
        image = NSImage(systemSymbolName: checked ? "checkmark.circle.fill" : "circle",
                        accessibilityDescription: checked ? "已选中" : "未选中")
        contentTintColor = checked ? MT.teal : MT.faint
        isEnabled = active
        alphaValue = active ? 1 : 0.3
        target = self
        action = #selector(fire)
        translatesAutoresizingMaskIntoConstraints = false
        widthAnchor.constraint(equalToConstant: 14).isActive = true
        heightAnchor.constraint(equalToConstant: 14).isActive = true
        toolTip = active ? "勾选后可批量结束" : "受保护"
    }

    @objc private func fire() { handler?() }
}

final class MTIconView: NSImageView {
    init(bundle: String?, name: String, size: CGFloat = 20) {
        super.init(frame: NSRect(x: 0, y: 0, width: size, height: size))
        translatesAutoresizingMaskIntoConstraints = false
        widthAnchor.constraint(equalToConstant: size).isActive = true
        heightAnchor.constraint(equalToConstant: size).isActive = true
        imageScaling = .scaleProportionallyUpOrDown
        if let bundle = bundle, FileManager.default.fileExists(atPath: bundle) {
            image = NSWorkspace.shared.icon(forFile: bundle)
        } else {
            image = NSImage(systemSymbolName: "gearshape.fill", accessibilityDescription: nil)
            contentTintColor = MT.faint
        }
    }

    required init?(coder: NSCoder) { fatalError() }
}

// MARK: - 面板

final class PanelView: NSView {
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        MT.bg.setFill()
        bounds.fill()
    }
}

final class PanelController: NSViewController {
    private let statusTitle = MT.label("MemTop", size: 12, weight: .semibold)
    private let pressureLabel = MT.label("压力 —", size: 10.5, color: MT.dim)
    private let pressureDot = NSView()
    private let usedBig = MT.label("--", size: 34, weight: .semibold, mono: true)
    private let usedUnit = MT.label("GB", size: 11, color: MT.faint)
    private let totalLabel = MT.label("— / —", size: 11, color: MT.dim, mono: true)
    private let pctLabel = MT.label("--%", size: 16, weight: .medium, color: MT.text, mono: true)
    private let bar = MTBar()
    private let legend = MT.label("", size: 10, color: MT.faint, mono: true)
    private let listStack = MTStack()
    private let scroll = NSScrollView()
    private let footNote = MT.label("—", size: 10, color: MT.faint)
    private var batchBar: NSView!
    private var batchLabel: NSTextField!
    private var batchKillButton: MTKillAllButton!
    private var batchHeight: NSLayoutConstraint!

    private var expanded = Set<String>()
    private var selected = Set<Int>()
    private var sortByCompressed = false
    private var explainedCompressed = false
    private var sortButton: MTButton!
    private var snapshot: MTSnapshot?
    private var timer: Timer?
    private var loading = false
    private var lastError: String?

    var memtopPath: String = MemTopBar.resolveMemtop()
    var onQuit: (() -> Void)?
    var debugExpand: Int?
    private(set) var isAlerting = false
    var debugSelect = 0

    override func loadView() {
        let root = PanelView(frame: NSRect(x: 0, y: 0, width: 360, height: 604))
        root.wantsLayer = true
        view = root

        // ---- 顶部：图标 + 标题 + 压力
        let mark = NSImageView()
        mark.image = NSApp.applicationIconImage
        mark.imageScaling = .scaleProportionallyUpOrDown
        mark.translatesAutoresizingMaskIntoConstraints = false
        mark.widthAnchor.constraint(equalToConstant: 22).isActive = true
        mark.heightAnchor.constraint(equalToConstant: 22).isActive = true

        let title = MT.label("MemTop", size: 12.5, weight: .semibold)
        let subtitle = MT.label("内存速查", size: 10, color: MT.faint)
        let titleBox = NSStackView(views: [title, subtitle])
        titleBox.orientation = .horizontal
        titleBox.spacing = 6
        titleBox.alignment = .firstBaseline

        pressureDot.wantsLayer = true
        pressureDot.translatesAutoresizingMaskIntoConstraints = false
        pressureDot.layer?.cornerRadius = 3
        pressureDot.widthAnchor.constraint(equalToConstant: 6).isActive = true
        pressureDot.heightAnchor.constraint(equalToConstant: 6).isActive = true

        let pressure = NSStackView(views: [pressureDot, pressureLabel])
        pressure.orientation = .horizontal
        pressure.spacing = 5
        pressure.alignment = .centerY

        let headerSpacer = NSView()
        headerSpacer.setContentHuggingPriority(.init(1), for: .horizontal)
        let brand = NSStackView(views: [mark, titleBox])
        brand.orientation = .horizontal
        brand.spacing = 7
        brand.alignment = .centerY

        let header = NSStackView(views: [brand, headerSpacer, pressure])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.translatesAutoresizingMaskIntoConstraints = false

        // ---- 大数字
        let bigRow = NSStackView(views: [usedBig, usedUnit])
        bigRow.orientation = .horizontal
        bigRow.spacing = 3
        bigRow.alignment = .firstBaseline

        let rightCol = NSStackView(views: [pctLabel, totalLabel])
        rightCol.orientation = .vertical
        rightCol.spacing = 1
        rightCol.alignment = .trailing
        rightCol.setContentHuggingPriority(.required, for: .horizontal)
        rightCol.widthAnchor.constraint(greaterThanOrEqualToConstant: 96).isActive = true
        pctLabel.setContentCompressionResistancePriority(.required, for: .horizontal)
        totalLabel.setContentCompressionResistancePriority(.required, for: .horizontal)
        pctLabel.alignment = .right
        totalLabel.alignment = .right
        pctLabel.widthAnchor.constraint(equalToConstant: 68).isActive = true
        totalLabel.widthAnchor.constraint(equalToConstant: 88).isActive = true
        usedBig.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let bigSpacer = NSView()
        bigSpacer.setContentHuggingPriority(.init(1), for: .horizontal)
        let big = NSStackView(views: [bigRow, bigSpacer, rightCol])
        big.orientation = .horizontal
        big.alignment = .bottom
        big.translatesAutoresizingMaskIntoConstraints = false

        // ---- 构成条
        bar.translatesAutoresizingMaskIntoConstraints = false
        bar.heightAnchor.constraint(equalToConstant: 8).isActive = true
        legend.translatesAutoresizingMaskIntoConstraints = false

        // ---- 列表
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.translatesAutoresizingMaskIntoConstraints = false

        listStack.orientation = .vertical
        listStack.spacing = 1
        listStack.alignment = .width
        listStack.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = listStack

        // ---- 脚注
        let openButton = MTButton("仪表盘") { [weak self] in self?.openDashboard() }
        openButton.setContentCompressionResistancePriority(.required, for: .horizontal)
        let refreshButton = MTButton("刷新") { [weak self] in self?.refresh(force: true) }
        let quitButton = MTButton("退出", tint: MT.faint) { [weak self] in self?.onQuit?() }
        sortButton = MTButton("按压缩", size: 9.5, tint: MT.dim) { [weak self] in self?.toggleSortMode() }
        sortButton.toolTip = "按压缩内存占用排序（结束进程可释放）"
        sortButton.setContentCompressionResistancePriority(.required, for: .horizontal)
        refreshButton.setContentCompressionResistancePriority(.required, for: .horizontal)
        quitButton.setContentCompressionResistancePriority(.required, for: .horizontal)
        footNote.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let footSpacer = NSView()
        footSpacer.setContentHuggingPriority(.init(1), for: .horizontal)
        let foot = NSStackView(views: [openButton, footNote, footSpacer, sortButton, refreshButton, quitButton])
        foot.orientation = .horizontal
        foot.alignment = .centerY
        foot.spacing = 8
        foot.translatesAutoresizingMaskIntoConstraints = false

        let divider1 = NSBox()
        divider1.boxType = .separator
        divider1.translatesAutoresizingMaskIntoConstraints = false
        let divider2 = NSBox()
        divider2.boxType = .separator
        divider2.translatesAutoresizingMaskIntoConstraints = false

        // 常驻批量操作条（有勾选时才显示）
        batchBar = NSView()
        batchBar.translatesAutoresizingMaskIntoConstraints = false
        batchBar.wantsLayer = true
        batchBar.isHidden = true
        batchBar.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.045).cgColor
        batchBar.layer?.cornerRadius = 8

        batchLabel = MT.label("", size: 10, color: MT.dim)
        let clearSelection = MTButton("清空", size: 9.5, tint: MT.faint) { [weak self] in
            guard let self = self else { return }
            self.selected.removeAll()
            if let snap = self.snapshot { self.rebuildList(Array(snap.groups.prefix(40))) }
        }
        batchKillButton = MTKillAllButton("结束选中", width: 62) { [weak self] in
            self?.batchKillSelected()
        }
        let batchSpacer = NSView()
        batchSpacer.setContentHuggingPriority(.init(1), for: .horizontal)
        let batchStack = NSStackView(views: [batchLabel, batchSpacer, clearSelection, batchKillButton])
        batchStack.orientation = .horizontal
        batchStack.alignment = .centerY
        batchStack.spacing = 10
        batchStack.translatesAutoresizingMaskIntoConstraints = false
        batchBar.addSubview(batchStack)
        NSLayoutConstraint.activate([
            batchStack.leadingAnchor.constraint(equalTo: batchBar.leadingAnchor, constant: 10),
            batchStack.trailingAnchor.constraint(equalTo: batchBar.trailingAnchor, constant: -6),
            batchStack.centerYAnchor.constraint(equalTo: batchBar.centerYAnchor),
        ])

        for v in [header, big, bar, legend, divider1, scroll, batchBar!, divider2, foot] {
            root.addSubview(v)
        }
        batchHeight = batchBar.heightAnchor.constraint(equalToConstant: 0)
        batchHeight.isActive = true

        let pad: CGFloat = 16
        NSLayoutConstraint.activate([
            header.topAnchor.constraint(equalTo: root.topAnchor, constant: 14),
            header.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: pad),
            header.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -pad),

            big.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 12),
            big.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: pad),
            big.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -pad),

            bar.topAnchor.constraint(equalTo: big.bottomAnchor, constant: 10),
            bar.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: pad),
            bar.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -pad),

            legend.topAnchor.constraint(equalTo: bar.bottomAnchor, constant: 7),
            legend.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: pad),
            legend.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -pad),

            divider1.topAnchor.constraint(equalTo: legend.bottomAnchor, constant: 12),
            divider1.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: pad),
            divider1.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -pad),
            divider1.heightAnchor.constraint(equalToConstant: 1),

            scroll.topAnchor.constraint(equalTo: divider1.bottomAnchor, constant: 8),
            scroll.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 10),
            scroll.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -10),
            scroll.bottomAnchor.constraint(equalTo: batchBar.topAnchor, constant: -6),

            listStack.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            listStack.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            listStack.trailingAnchor.constraint(equalTo: scroll.contentView.trailingAnchor),

            batchBar.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 12),
            batchBar.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -12),
            batchBar.bottomAnchor.constraint(equalTo: divider2.topAnchor, constant: -8),
            batchHeight,

            divider2.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: pad),
            divider2.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -pad),
            divider2.heightAnchor.constraint(equalToConstant: 1),

            foot.topAnchor.constraint(equalTo: divider2.bottomAnchor, constant: 9),
            foot.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: pad - 2),
            foot.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -pad + 2),
            foot.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -12),
            foot.heightAnchor.constraint(equalToConstant: 18),

            root.widthAnchor.constraint(equalToConstant: 360),
            root.heightAnchor.constraint(equalToConstant: 604),
        ])

        pressureDot.layer?.backgroundColor = MT.green.cgColor
    }

    override func viewDidLoad() {
        super.viewDidLoad()
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        refresh(force: true)
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.refresh(force: false)
        }
        if let t = timer { RunLoop.main.add(t, forMode: .common) }
    }

    override func viewWillDisappear() {
        super.viewWillDisappear()
        timer?.invalidate()
        timer = nil
    }

    deinit { timer?.invalidate() }

    // MARK: 数据

    func refresh(force: Bool) {
        if loading { return }
        loading = true
        let path = memtopPath
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = MemTopBar.runJSON(path)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.loading = false
                switch result {
                case .success(let snap):
                    self.lastError = nil
                    self.apply(snap)
                case .failure(let err):
                    self.lastError = err.message
                    self.showError(err.message)
                }
            }
        }
    }

    private func apply(_ snap: MTSnapshot) {
        snapshot = snap
        let mem = snap.memory
        let pct = mem.total > 0 ? Double(mem.used) / Double(mem.total) * 100 : 0

        usedBig.stringValue = MT.size(mem.used).replacingOccurrences(of: " GB", with: "")
            .replacingOccurrences(of: " MB", with: "")
        let unit = MT.size(mem.used).components(separatedBy: " ").last ?? "GB"
        usedUnit.stringValue = unit
        totalLabel.stringValue = "共 " + MT.size(mem.total)
        pctLabel.stringValue = String(format: "%.0f%%", pct)

        switch mem.pressure {
        case 2:
            pctLabel.textColor = MT.amber
            pressureLabel.textColor = MT.amber
            pressureDot.layer?.backgroundColor = MT.amber.cgColor
        case 4:
            pctLabel.textColor = MT.red
            pressureLabel.textColor = MT.red
            pressureDot.layer?.backgroundColor = MT.red.cgColor
        default:
            pctLabel.textColor = MT.text
            pressureLabel.textColor = MT.dim
            pressureDot.layer?.backgroundColor = MT.green.cgColor
        }
        pressureLabel.stringValue = "压力 " + mem.pressure_label

        bar.segments = [
            (Double(mem.app) / Double(max(1, mem.total)), MT.teal),
            (Double(mem.wired) / Double(max(1, mem.total)), MT.blue),
            (Double(mem.compressed) / Double(max(1, mem.total)), MT.violet),
            (Double(mem.cached) / Double(max(1, mem.total)), NSColor.white.withAlphaComponent(0.22)),
        ]
        legend.stringValue = "应用 \(MT.size(mem.app)) · 有线 \(MT.size(mem.wired)) · 压缩 \(MT.size(mem.compressed)) · 缓存 \(MT.size(mem.cached))"
        footNote.stringValue = "\(snap.processes) 进程 · 压缩可释放 \(MT.size(mem.compressed_killable ?? 0))"
        footNote.toolTip = "可结束进程当前占用的压缩页合计；结束它们才会回收这部分内存"

        let top = Array(snap.groups.prefix(40))
        let ordered = sortByCompressed
            ? top.sorted { $0.compressed > $1.compressed }
            : top
        rebuildList(ordered)
        if let idx = debugExpand, idx < ordered.count {
            debugExpand = nil
            expanded.insert(ordered[idx].key)
            if debugSelect > 0 {
                let g = ordered[idx]
                selected.formUnion(g.members.filter { !$0.protected }.prefix(debugSelect).map { $0.pid })
            }
            rebuildList(top)
        }
        statusTitle.stringValue = "MemTop"
    }

    private func showError(_ message: String) {
        pressureLabel.stringValue = "读取失败"
        pressureLabel.textColor = MT.red
        pressureDot.layer?.backgroundColor = MT.red.cgColor
        if listStack.arrangedSubviews.isEmpty {
            let wrap = MT.label(message, size: 11, color: MT.red)
            wrap.maximumNumberOfLines = 3
            wrap.lineBreakMode = .byWordWrapping
            listStack.addArrangedSubview(wrap)
        }
    }

    private func rebuildList(_ groups: [MTGroup]) {
        for v in listStack.arrangedSubviews {
            listStack.removeArrangedSubview(v)
            v.removeFromSuperview()
        }
        selected.formIntersection(Set(groups.flatMap { $0.members.map { $0.pid } }))
        for (idx, g) in groups.enumerated() {
            listStack.addArrangedSubview(groupRow(g, index: idx))
            if expanded.contains(g.key) {
                if g.members.count > 1 && g.killable > 0 {
                    listStack.addArrangedSubview(selectAllHeader(g))
                }
                for m in g.members {
                    listStack.addArrangedSubview(memberRow(m, group: g))
                }
            }
        }
        if groups.isEmpty {
            listStack.addArrangedSubview(MT.label("没有进程", size: 11, color: MT.faint))
        }
        updateBatchBar()
    }

    private func groupRow(_ g: MTGroup, index: Int) -> NSView {
        let row = MTRow()
        row.translatesAutoresizingMaskIntoConstraints = false
        row.heightAnchor.constraint(equalToConstant: 44).isActive = true
        row.wantsLayer = true
        row.layer?.cornerRadius = 7

        let chevron = NSImageView()
        chevron.image = NSImage(systemSymbolName: expanded.contains(g.key) ? "chevron.down" : "chevron.right",
                                accessibilityDescription: nil)
        chevron.contentTintColor = MT.faint
        chevron.translatesAutoresizingMaskIntoConstraints = false
        chevron.widthAnchor.constraint(equalToConstant: 9).isActive = true
        chevron.isHidden = g.members.count <= 1

        let icon = MTIconView(bundle: g.bundle, name: g.name, size: 20)

        let name = MT.label(g.name, size: 12.5, weight: .medium)
        let subText = sortByCompressed
            ? "共 \(MT.size(g.footprint)) · \(g.members.count) 个进程"
            : "\(g.members.count) 个进程"
        let sub = MT.label(subText + (g.risk == "caution" ? " · 慎" : ""),
                           size: 9.5, color: g.risk == "caution" ? MT.amber : MT.faint)
        let nameBox = NSStackView(views: [name, sub])
        nameBox.orientation = .vertical
        nameBox.spacing = 0
        nameBox.alignment = .leading

        let shownValue = sortByCompressed ? g.compressed : g.footprint
        let size = MT.label(MT.size(shownValue), size: 11.5, weight: .medium, mono: true)
        size.alignment = .right
        size.widthAnchor.constraint(equalToConstant: 58).isActive = true

        let kill = MTKillAllButton("清除") { [weak self] in
            self?.clearAll(group: g)
        }
        kill.toolTip = g.killable > 0
            ? "立即结束 \(g.name) 的全部可结束进程（不再二次确认）"
            : "受保护，不可结束"
        kill.isHidden = g.killable == 0

        let spacer = NSView()
        spacer.setContentHuggingPriority(.init(1), for: .horizontal)

        let stack = NSStackView(views: [chevron, icon, nameBox, spacer, size, kill])
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 7
        stack.translatesAutoresizingMaskIntoConstraints = false
        row.addSubview(stack)

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: row.leadingAnchor, constant: 6),
            stack.trailingAnchor.constraint(equalTo: row.trailingAnchor, constant: -6),
            stack.centerYAnchor.constraint(equalTo: row.centerYAnchor),
        ])

        let click = NSClickGestureRecognizer(target: self, action: #selector(toggleGroup(_:)))
        row.addGestureRecognizer(click)
        row.identifier = NSUserInterfaceItemIdentifier(g.key)
        row.toolTip = "\(g.name) · \(MT.size(g.footprint))，点击展开"

        if g.killable == 0 {
            row.alphaValue = 0.55
        }
        return row
    }

    @objc private func toggleGroup(_ sender: NSClickGestureRecognizer) {
        guard let row = sender.view, let key = row.identifier?.rawValue else { return }
        // 点在“清除”按钮上时，不要让整行跟着展开/收起
        if let hit = row.hitTest(sender.location(in: row)), hit is NSButton { return }
        if expanded.contains(key) { expanded.remove(key) } else { expanded.insert(key) }
        if let snap = snapshot { rebuildList(Array(snap.groups.prefix(40))) }
    }

    private func memberRow(_ m: MTMember, group: MTGroup) -> NSView {
        let row = MTRow()
        row.translatesAutoresizingMaskIntoConstraints = false
        row.heightAnchor.constraint(equalToConstant: 26).isActive = true
        row.identifier = NSUserInterfaceItemIdentifier("m:\(m.pid)")
        row.toolTip = m.protected ? (m.reason.isEmpty ? "受保护" : m.reason) : "点击勾选，可批量结束"

        let pick = MTSelectButton(checked: selected.contains(m.pid), active: !m.protected) { [weak self] in
            self?.toggleSelection(m.pid)
        }

        let name = MT.label(m.name, size: 10.5, color: MT.dim)
        name.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let pid = MT.label(String(m.pid), size: 9, color: MT.faint, mono: true)
        pid.widthAnchor.constraint(equalToConstant: 42).isActive = true

        let tag = MT.label(m.protected ? (m.reason.isEmpty ? "受保护" : m.reason) : "",
                           size: 9, color: MT.faint)
        let size = MT.label(MT.size(m.footprint), size: 10, color: MT.dim, mono: true)
        size.alignment = .right
        size.setContentCompressionResistancePriority(.required, for: .horizontal)
        size.widthAnchor.constraint(equalToConstant: 60).isActive = true

        let kill = MTButton("✕", size: 10, tint: MT.red) { [weak self] in
            self?.confirmKill(member: m, group: group)
        }
        kill.widthAnchor.constraint(equalToConstant: 20).isActive = true
        kill.isHidden = m.protected

        let spacer = NSView()
        spacer.setContentHuggingPriority(.init(1), for: .horizontal)

        let stack = NSStackView(views: [pick, name, pid, tag, spacer, size, kill])
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 6
        stack.translatesAutoresizingMaskIntoConstraints = false
        row.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: row.leadingAnchor, constant: 26),
            stack.trailingAnchor.constraint(equalTo: row.trailingAnchor, constant: -6),
            stack.centerYAnchor.constraint(equalTo: row.centerYAnchor),
        ])

        let click = NSClickGestureRecognizer(target: self, action: #selector(toggleMemberRow(_:)))
        row.addGestureRecognizer(click)
        return row
    }

    // MARK: 批量选择

    private func toggleSelection(_ pid: Int) {
        if selected.contains(pid) { selected.remove(pid) } else { selected.insert(pid) }
        if let snap = snapshot { rebuildList(Array(snap.groups.prefix(40))) }
    }

    @objc private func toggleMemberRow(_ sender: NSClickGestureRecognizer) {
        guard let row = sender.view, let id = row.identifier?.rawValue, id.hasPrefix("m:"),
              let pid = Int(id.dropFirst(2)) else { return }
        // 点在勾选框或 ✕ 上时交给控件自己处理
        if let hit = row.hitTest(sender.location(in: row)), hit is NSButton { return }
        if let m = snapshot?.groups.flatMap({ $0.members }).first(where: { $0.pid == pid }), m.protected {
            return
        }
        toggleSelection(pid)
    }

    private func selectAllHeader(_ g: MTGroup) -> NSView {
        let killable = g.members.filter { !$0.protected }
        let chosen = killable.filter { selected.contains($0.pid) }.count
        let all = !killable.isEmpty && chosen == killable.count

        let row = NSView()
        row.translatesAutoresizingMaskIntoConstraints = false
        row.heightAnchor.constraint(equalToConstant: 22).isActive = true

        let toggle = MTButton(all ? "取消全选" : "全选", size: 9.5, tint: MT.teal) { [weak self] in
            self?.toggleSelectAll(g)
        }
        let note = MT.label("\(killable.count) 个可结束" + (chosen > 0 ? " · 已选 \(chosen)" : ""),
                            size: 9, color: chosen > 0 ? MT.dim : MT.faint)

        let spacer = NSView()
        spacer.setContentHuggingPriority(.init(1), for: .horizontal)
        let stack = NSStackView(views: [toggle, note, spacer])
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 7
        stack.translatesAutoresizingMaskIntoConstraints = false
        row.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: row.leadingAnchor, constant: 26),
            stack.trailingAnchor.constraint(equalTo: row.trailingAnchor, constant: -6),
            stack.centerYAnchor.constraint(equalTo: row.centerYAnchor),
        ])
        return row
    }

    private func toggleSelectAll(_ g: MTGroup) {
        let pids = g.members.filter { !$0.protected }.map { $0.pid }
        if pids.allSatisfy({ selected.contains($0) }) {
            selected.subtract(pids)
        } else {
            selected.formUnion(pids)
        }
        if let snap = snapshot { rebuildList(Array(snap.groups.prefix(40))) }
    }

    private func selectedMembers() -> [MTMember] {
        guard let snap = snapshot else { return [] }
        return snap.groups.flatMap { $0.members }
            .filter { !$0.protected && selected.contains($0.pid) }
    }

    private func updateBatchBar() {
        let chosen = selectedMembers()
        let visible = !chosen.isEmpty
        batchHeight.constant = visible ? 34 : 0
        batchBar.isHidden = !visible
        let total = chosen.reduce(0) { $0 + $1.footprint }
        batchLabel.stringValue = visible ? "已选 \(chosen.count) 个 · ≈ \(MT.size(total))" : ""
        batchKillButton.isEnabled = visible
        batchKillButton.alphaValue = visible ? 1 : 0.35
    }

    func enableCompressedSort() {
        sortByCompressed = true
        sortButton?.setTitle("按内存", tint: MT.teal)
        if let snap = snapshot {
            let top = Array(snap.groups.prefix(40))
            rebuildList(top.sorted { $0.compressed > $1.compressed })
        }
    }

    private func toggleSortMode() {
        sortByCompressed.toggle()
        sortButton.setTitle(sortByCompressed ? "按内存" : "按压缩", tint: sortByCompressed ? MT.teal : MT.dim)
        if let snap = snapshot {
            let top = Array(snap.groups.prefix(40))
            rebuildList(sortByCompressed ? top.sorted { $0.compressed > $1.compressed } : top)
        }
        if sortByCompressed && !explainedCompressed {
            explainedCompressed = true
            alert("按压缩排序",
                  "压缩内存由内核管理，用户态无法直接释放。这里按每个应用占用的压缩页排序；结束进程后，它占用的压缩页才会被回收。",
                  style: .informational)
        }
    }

    private func batchKillSelected() {
        let targets = selectedMembers()
        guard !targets.isEmpty else { return }
        targets.forEach { selected.remove($0.pid) }
        kill(pids: targets.map { $0.pid }, label: "选中的 \(targets.count) 个进程")
    }

    // MARK: 结束

    private func clearAll(group: MTGroup) {
        let targets = group.members.filter { !$0.protected }
        guard !targets.isEmpty else {
            alert("受保护", "\(group.name) 的进程全部在保护名单里，不能结束。", style: .informational)
            return
        }
        // 点了“清除”就直接执行：不再做第二次进程确认
        targets.forEach { selected.remove($0.pid) }
        kill(pids: targets.map { $0.pid }, label: group.name)
    }

    private func confirmKill(member: MTMember, group: MTGroup) {
        guard !member.protected else { return }
        var info = "PID \(member.pid) · \(MT.size(member.footprint))"
        if member.risk == "caution" {
            info += "\n\n这是应用主进程，可能有未保存的内容。"
        }
        alert("结束 \(member.name)？", info, style: member.risk == "caution" ? .warning : .informational,
              confirm: "结束", destructive: true) { [weak self] in
            self?.kill(pids: [member.pid], label: member.name)
        }
    }

    private func kill(pids: [Int], label: String) {
        let path = memtopPath
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let outcome = MemTopBar.runKill(path, pids: pids)
            DispatchQueue.main.async {
                guard let self = self else { return }
                switch outcome {
                case .success:
                    self.refresh(force: true)
                case .survivors:
                    self.alert("没有退出", "\(label) 没有响应 SIGTERM。强制结束会立即终止进程，未保存内容会丢失。",
                               style: .warning, confirm: "强制结束", destructive: true) {
                        let forced = MemTopBar.runKill(path, pids: pids, force: true)
                        if case .failure(let msg) = forced {
                            self.alert("强制结束失败", msg, style: .critical)
                        }
                        self.refresh(force: true)
                    }
                case .failure(let msg):
                    self.alert("结束失败", msg, style: .critical)
                }
            }
        }
    }

    private func alert(_ title: String, _ info: String, style: NSAlert.Style,
                       confirm: String? = nil, destructive: Bool = false,
                       then: (() -> Void)? = nil) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = info
        a.alertStyle = style
        if let confirm = confirm {
            a.addButton(withTitle: confirm)
            a.addButton(withTitle: "取消")
            if destructive {
                a.buttons.first?.hasDestructiveAction = true
            }
        } else {
            a.addButton(withTitle: "好")
        }
        // 面板是 .statusBar 层级，会盖住系统弹框（弹框固定为 modalPanel=8）。
        // 弹框期间把面板降到弹框下面一层，弹框结束后恢复。
        let panels = NSApp.windows.compactMap { $0 as? PanelWindow }
        let saved = panels.map { ($0, $0.level) }
        panels.forEach { $0.level = NSWindow.Level(rawValue: NSWindow.Level.modalPanel.rawValue - 1) }
        NSApp.activate(ignoringOtherApps: true)

        isAlerting = true
        defer {
            isAlerting = false
            saved.forEach { $0.0.level = $0.1 }
        }
        let resp = a.runModal()
        if confirm != nil, resp == .alertFirstButtonReturn { then?() }
    }

    func debugShowAlert() {
        alert("测试弹框", "这个弹框应该盖在面板上方，并且可以点击。", style: .warning,
              confirm: "好", destructive: false)
    }

    // MARK: 仪表盘

    private func openDashboard() {
        let url = URL(string: "http://127.0.0.1:8787/")!
        var req = URLRequest(url: url)
        req.timeoutInterval = 0.6
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                DispatchQueue.main.async { NSWorkspace.shared.open(url) }
                return
            }
            DispatchQueue.main.async { self?.startDashboard() }
        }.resume()
    }

    private func startDashboard() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: memtopPath)
        p.arguments = ["web", "--port", "8787", "--no-open"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
        } catch {
            alert("打不开仪表盘", "无法启动 \(memtopPath) web：\(error.localizedDescription)", style: .critical)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) {
            NSWorkspace.shared.open(URL(string: "http://127.0.0.1:8787/")!)
        }
    }
}

extension MTGroup {
    var key: String { "app:" + (bundle ?? name) }
}

// 行：悬停高亮
final class MTRow: NSView {
    private var tracking: NSTrackingArea?

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(rect: bounds,
                               options: [.mouseEnteredAndExited, .activeAlways],
                               owner: self, userInfo: nil)
        addTrackingArea(t)
        tracking = t
    }

    override func mouseEntered(with event: NSEvent) {
        layer?.backgroundColor = NSColor.white.withAlphaComponent(0.05).cgColor
    }

    override func mouseExited(with event: NSEvent) {
        layer?.backgroundColor = NSColor.clear.cgColor
    }
}

final class MTStack: NSStackView {
    override var isFlipped: Bool { true }
}

// MARK: - 菜单栏 App

final class PanelWindow: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 {  // Esc 关闭
            orderOut(nil)
            return
        }
        super.keyDown(with: event)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var panel: PanelWindow?
    private var controller: PanelController!
    private var refreshTimer: Timer?
    private var localMonitor: Any?
    private var globalMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "memorychip", accessibilityDescription: "MemTop")
            button.image?.isTemplate = true
            button.imagePosition = .imageLeading
            button.font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)
            button.title = " …"
            button.target = self
            button.action = #selector(togglePanelAction(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }

        controller = PanelController()
        controller.onQuit = { NSApp.terminate(nil) }

        refreshStatusTitle()
        if CommandLine.arguments.contains("--show") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
                self?.showPanel()
            }
        }
        if CommandLine.arguments.contains("--alert-test") {
            debugAlert()
        }
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.refreshStatusTitle()
        }
        RunLoop.main.add(refreshTimer!, forMode: .common)
    }

    // MARK: 面板

    private func makePanelIfNeeded() -> PanelWindow {
        if let panel = panel { return panel }

        let p = PanelWindow(contentRect: NSRect(x: 0, y: 0, width: 360, height: 604),
                            styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        p.level = .statusBar
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.isReleasedWhenClosed = false
        p.hidesOnDeactivate = false
        p.isMovable = false
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        p.animationBehavior = .utilityWindow
        p.contentViewController = controller
        if let v = p.contentView {
            v.wantsLayer = true
            v.layer?.cornerRadius = 16
            v.layer?.masksToBounds = true
            v.layer?.borderWidth = 1
            v.layer?.borderColor = NSColor.white.withAlphaComponent(0.12).cgColor
        }
        panel = p
        return p
    }

    private func positionPanel(_ p: PanelWindow) {
        let size = p.frame.size
        // 用鼠标所在屏定位：点菜单栏图标时鼠标就在那块屏的菜单栏上，
        // URL scheme / 双击启动时也会出现在用户正在看的那块屏。
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) }
            ?? NSScreen.main ?? NSScreen.screens[0]
        let vf = screen.visibleFrame

        var x = mouse.x - size.width / 2
        x = min(max(x, vf.minX + 10), vf.maxX - size.width - 10)
        let y = vf.maxY - size.height - 6
        p.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height), display: true)
    }

    func showPanel() {
        let p = makePanelIfNeeded()
        positionPanel(p)
        NSApp.activate(ignoringOtherApps: true)
        p.makeKeyAndOrderFront(nil)
        controller.refresh(force: true)
        installMonitors()
    }

    func hidePanel() {
        panel?.orderOut(nil)
    }

    private func installMonitors() {
        if localMonitor == nil {
            localMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] event in
                guard let self = self, let p = self.panel, p.isVisible else { return event }
                if self.controller.isAlerting { return event }
                if event.window === p || event.window === self.statusItem.button?.window {
                    return event
                }
                self.hidePanel()
                return event
            }
        }
        if globalMonitor == nil {
            globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
                guard let self = self else { return }
                if self.controller.isAlerting { return }
                self.hidePanel()
            }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPanel()
        return true
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        if urls.contains(where: { $0.scheme == "memtop" }) {
            showPanel()
        }
    }

    @objc private func togglePanelAction(_ sender: Any?) {
        guard let button = statusItem.button else { return }
        if let event = NSApp.currentEvent, event.type == .rightMouseUp {
            let menu = NSMenu()
            menu.addItem(withTitle: "打开面板", action: #selector(openPanel), keyEquivalent: "").target = self
            menu.addItem(withTitle: "刷新", action: #selector(forceRefresh), keyEquivalent: "r").target = self
            menu.addItem(.separator())
            menu.addItem(withTitle: "退出 MemTopBar", action: #selector(quit), keyEquivalent: "q").target = self
            statusItem.menu = menu
            button.performClick(nil)
            statusItem.menu = nil
            return
        }
        if panel?.isVisible == true {
            hidePanel()
        } else {
            showPanel()
        }
    }

    @objc private func openPanel() { showPanel() }

    func debugAlert() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
            self?.controller.debugShowAlert()
        }
    }

    @objc private func forceRefresh() { controller.refresh(force: true) }

    @objc private func quit() { NSApp.terminate(nil) }

    private func refreshStatusTitle() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = MemTopBar.runJSON(MemTopBar.resolveMemtop())
            guard case .success(let snap) = result else { return }
            let mem = snap.memory
            let pct = mem.total > 0 ? Int((Double(mem.used) / Double(mem.total) * 100).rounded()) : 0
            let title = "\(pct)%  " + MT.size(mem.used).replacingOccurrences(of: " GB", with: "G")
                .replacingOccurrences(of: " MB", with: "M")
            DispatchQueue.main.async {
                self?.statusItem.button?.title = title
                self?.statusItem.button?.toolTip = "已用 \(MT.size(mem.used)) / \(MT.size(mem.total)) · 压力 \(mem.pressure_label)"
            }
        }
    }
}

// MARK: - 进程调用

enum MemTopBar {
    static func resolveMemtop() -> String {
        if let env = ProcessInfo.processInfo.environment["MEMTOP_BIN"],
           FileManager.default.isExecutableFile(atPath: env) {
            return env
        }
        let candidates = [
            NSString(string: "~/.local/bin/memtop").expandingTildeInPath,
            "/usr/local/bin/memtop",
            "/opt/homebrew/bin/memtop",
        ]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            return c
        }
        return NSString(string: "~/.local/bin/memtop").expandingTildeInPath
    }

    static func runJSON(_ path: String) -> Result<MTSnapshot, MTFailure> {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = ["--json"]
        let out = Pipe()
        let err = Pipe()
        p.standardOutput = out
        p.standardError = err
        do {
            try p.run()
        } catch {
            return .failure(MTFailure(message: "无法运行 \(path)：\(error.localizedDescription)"))
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else {
            let msg = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            return .failure(MTFailure(message: "memtop 退出码 \(p.terminationStatus)：\(msg)"))
        }
        do {
            let snap = try JSONDecoder().decode(MTSnapshot.self, from: data)
            return .success(snap)
        } catch {
            return .failure(MTFailure(message: "解析快照失败：\(error.localizedDescription)"))
        }
    }

    enum KillOutcome {
        case success
        case survivors
        case failure(String)
    }

    static func runKill(_ path: String, pids: [Int], force: Bool = false) -> KillOutcome {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        var args = ["kill"] + pids.map(String.init) + ["--yes", "--grace", "3"]
        if force { args.append("--force") }
        p.arguments = args
        let out = Pipe()
        let err = Pipe()
        p.standardOutput = out
        p.standardError = err
        do {
            try p.run()
        } catch {
            return .failure("无法运行 memtop kill：\(error.localizedDescription)")
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        switch p.terminationStatus {
        case 0:
            return .success
        case 1:
            return .survivors
        default:
            let msg = String(data: errData, encoding: .utf8) ?? String(data: data, encoding: .utf8) ?? ""
            return .failure(msg.isEmpty ? "退出码 \(p.terminationStatus)" : msg)
        }
    }
}

// MARK: - 入口

let args = CommandLine.arguments
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate

if let i = args.firstIndex(of: "--kill-test"), i + 1 < args.count, let pid = Int(args[i + 1]) {
    let r = MemTopBar.runKill(MemTopBar.resolveMemtop(), pids: [pid])
    switch r {
    case .success: print("ok killed \(pid)")
    case .survivors: print("survivors \(pid)")
    case .failure(let m): print("failed: \(m)")
    }
    exit(0)
}

if let idx = args.firstIndex(of: "--snapshot"), idx + 1 < args.count {
    // 截图模式：渲染 360x604 面板，不显示菜单栏
    let outPath = args[idx + 1]
    let controller = PanelController()
    if let i = args.firstIndex(of: "--expand"), i + 1 < args.count, let n = Int(args[i + 1]) {
        controller.debugExpand = n
    }
    if let i = args.firstIndex(of: "--select"), i + 1 < args.count, let n = Int(args[i + 1]) {
        controller.debugSelect = n
    }
    let compressedMode = args.contains("--compressed")
    let v = controller.view
    v.frame = NSRect(x: 0, y: 0, width: 360, height: 604)
    v.layoutSubtreeIfNeeded()
    controller.refresh(force: true)
    if compressedMode {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { controller.enableCompressedSort() }
    }
    let wait = args.firstIndex(of: "--wait").flatMap { i -> Double? in
        i + 1 < args.count ? Double(args[i + 1]) : nil
    } ?? 2.6
    RunLoop.main.run(until: Date().addingTimeInterval(wait))
    v.layoutSubtreeIfNeeded()
    if args.contains("--debug-frames") {
        func walk(_ view: NSView, _ depth: Int) {
            if let tf = view as? NSTextField, !tf.stringValue.isEmpty {
                print(String(repeating: "  ", count: depth) + "\(type(of: view)) [\(tf.stringValue)] frame=\(tf.frame) intrinsic=\(tf.intrinsicContentSize)")
            }
            for sub in view.subviews { walk(sub, depth + 1) }
        }
        walk(v, 0)
    }
    if let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
        v.cacheDisplay(in: v.bounds, to: rep)
        if let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: outPath))
            print("wrote \(outPath)")
        }
    }
    exit(0)
}

app.run()
