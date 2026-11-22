// Makes phone-style photos of the paper version of each inbox invoice, for tests/test_scan.py.
//
//   swiftc -O tests/make_bills.swift -o /tmp/make_bills && /tmp/make_bills data/fixtures/inbox data/fixtures/bills
//
// Layout is a typical Indian GST invoice: vendor heading, invoice details in a right-hand column,
// a line-item table, a total, and bank details. Each photo gets its own tilt, light and blur.
import AppKit
import CoreImage

struct Look {
    let tilt: CGFloat; let ev: Double; let blur: Double; let shadow: Bool; let noise: Bool; let q: Double
    var keystone: CGFloat = 0   // > 0: shot from below at an angle, the top of the page narrower than the bottom
}
let looks: [(String, String, Look)] = [
    ("01_shadowfax_invoice", "01_shadowfax_invoice", Look(tilt: 2, ev: 0, blur: 0, shadow: true, noise: false, q: 0.8)),
    ("02_shreeji_invoice", "02_shreeji_invoice", Look(tilt: -1, ev: -1.0, blur: 0, shadow: false, noise: true, q: 0.7)),
    ("03_tci_invoice", "03_tci_invoice", Look(tilt: -3, ev: -0.3, blur: 1.0, shadow: false, noise: false, q: 0.8)),
    ("04_shadowfax_invoice_resent", "04_shadowfax_invoice_resent", Look(tilt: 1, ev: 0, blur: 0, shadow: true, noise: true, q: 0.6)),
    ("06_tci_invoice_new_account", "06_tci_invoice_new_account", Look(tilt: 1.5, ev: -0.5, blur: 0.5, shadow: false, noise: false, q: 0.4)),
    // harder, like a hand-held iPhone shot
    ("01_shadowfax_invoice", "10_hand_tilt7", Look(tilt: 7, ev: 0, blur: 0.4, shadow: true, noise: false, q: 0.7)),
    ("02_shreeji_invoice", "11_hand_keystone", Look(tilt: 2, ev: -0.4, blur: 0.4, shadow: false, noise: true, q: 0.7, keystone: 0.12)),
    ("06_tci_invoice_new_account", "12_hand_dark_tilt", Look(tilt: -5, ev: -1.4, blur: 0.6, shadow: true, noise: true, q: 0.6)),
    ("01_shadowfax_invoice", "99_unreadable", Look(tilt: 4, ev: -3.2, blur: 9, shadow: true, noise: true, q: 0.3)),
]

func fields(_ text: String) -> ([String: (String, String)], String) {
    var f: [String: (String, String)] = [:]
    var title = "TAX INVOICE"
    for raw in text.components(separatedBy: "\n") {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if line.hasPrefix("From:") || line.hasPrefix("Subject:") || line.isEmpty { continue }
        if line.hasPrefix("TAX INVOICE") { title = line; continue }
        if let r = line.range(of: "Account No: ") {
            let rest = line[r.upperBound...].components(separatedBy: " ").filter { !$0.isEmpty }
            f["acct"] = ("Account No:", rest[0])
            if let i = rest.firstIndex(of: "IFSC:") { f["ifsc"] = ("IFSC:", rest[i + 1]) }
            continue
        }
        guard let c = line.range(of: ": ") else { continue }
        let label = String(line[..<c.lowerBound]) + ":", value = String(line[c.upperBound...])
        let key: String
        switch label {
        case "Vendor:": key = "vendor"
        case "GSTIN:": key = "gstin"
        case "PO No:": key = "po"
        case "Description:": key = "desc"
        case "Qty:": key = "qty"
        case "Account Name:": key = "accname"
        default:
            if label.hasPrefix("Invoice N") { key = "inv" }
            else if label.hasSuffix("Date:") { key = "date" }
            else if label.contains("Total") || label.contains("Amount") { key = "amount" }
            else { continue }
        }
        f[key] = (label, value)
    }
    return (f, title)
}

func bitmap(_ w: Int, _ h: Int, flipped: Bool, _ draw: (CGContext) -> Void) -> CGImage {
    let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    if flipped { ctx.translateBy(x: 0, y: CGFloat(h)); ctx.scaleBy(x: 1, y: -1) }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: flipped)
    draw(ctx)
    NSGraphicsContext.restoreGraphicsState()
    return ctx.makeImage()!
}

func paper(_ f: [String: (String, String)], _ title: String) -> CGImage {
    bitmap(1240, 1754, flipped: true) { ctx in
        ctx.setFillColor(CGColor(red: 0.98, green: 0.97, blue: 0.94, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: 1240, height: 1754))
        func put(_ s: String, _ x: CGFloat, _ y: CGFloat, _ size: CGFloat = 24, bold: Bool = false, right: Bool = false) {
            let a: [NSAttributedString.Key: Any] = [.font: NSFont(name: bold ? "Helvetica-Bold" : "Helvetica", size: size)!, .foregroundColor: NSColor(white: 0.1, alpha: 1)]
            let w = (s as NSString).size(withAttributes: a).width
            (s as NSString).draw(at: NSPoint(x: right ? x - w : x, y: y), withAttributes: a)
        }
        func rule(_ y: CGFloat) { ctx.setStrokeColor(CGColor(gray: 0.3, alpha: 1)); ctx.stroke(CGRect(x: 80, y: y, width: 1080, height: 0.5)) }
        let v = f["vendor"]?.1 ?? ""
        put(v, 80, 80, 36, bold: true); put(title, 1160, 84, 32, bold: true, right: true)
        if let g = f["gstin"] { put("\(g.0) \(g.1)", 80, 140) }
        put("Original for recipient", 1160, 140, 18, right: true)
        rule(200)
        put("Bill To", 80, 240, 22, bold: true)
        put("Swiftline Logistics Pvt Ltd", 80, 280, 22); put("Plot 12, MIDC Bhosari, Pune 411026", 80, 318, 22)
        for (i, k) in ["inv", "date", "po"].enumerated() {
            if let p = f[k] { put(p.0, 660, 240 + CGFloat(i) * 40); put(p.1, 880, 240 + CGFloat(i) * 40) }
        }
        rule(410)
        put("Description", 80, 430, 22, bold: true); put("HSN/SAC", 700, 430, 22, bold: true); put("Amount", 1160, 430, 22, bold: true, right: true)
        rule(470)
        let desc = f["desc"]?.1 ?? "Freight services, Nov 2026"
        let amt = f["amount"]?.1 ?? ""
        put("\(desc) (Qty: \(f["qty"]?.1 ?? "1"))", 80, 490, 22); put("996812", 700, 490, 22); put(amt, 1160, 490, 22, right: true)
        rule(560)
        put(f["amount"]?.0 ?? "Total:", 560, 590, 26, bold: true); put(amt, 1160, 590, 26, bold: true, right: true)
        put("Bank details", 80, 720, 22, bold: true)
        if let a = f["acct"] { put("\(a.0) \(a.1)", 80, 760) }
        if let i = f["ifsc"] { put("\(i.0) \(i.1)", 660, 760) }
        if let n = f["accname"] { put("\(n.0) \(n.1)", 80, 800) }
        put("For \(v)", 1160, 960, 22, right: true); put("Authorised signatory", 1160, 1060, 20, right: true)
    }
}

func photo(_ p: CGImage, _ look: Look) -> CIImage {
    let desk = bitmap(1400, 1900, flipped: false) { ctx in
        ctx.setFillColor(CGColor(red: 0.35, green: 0.28, blue: 0.22, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: 1400, height: 1900))
        ctx.translateBy(x: 700, y: 950); ctx.rotate(by: look.tilt * .pi / 180)
        ctx.draw(p, in: CGRect(x: -620, y: -877, width: 1240, height: 1754))
    }
    var ci = CIImage(cgImage: desk)
    let ext = ci.extent
    if look.shadow {
        let g = CIFilter(name: "CILinearGradient", parameters: [
            "inputPoint0": CIVector(x: 0, y: 0), "inputPoint1": CIVector(x: ext.width * 0.7, y: ext.height * 0.6),
            "inputColor0": CIColor(red: 0, green: 0, blue: 0, alpha: 0.5), "inputColor1": CIColor(red: 0, green: 0, blue: 0, alpha: 0)])!
        ci = g.outputImage!.cropped(to: ext).composited(over: ci)
    }
    if look.ev != 0 { ci = ci.applyingFilter("CIExposureAdjust", parameters: ["inputEV": look.ev]) }
    if look.blur > 0 { ci = ci.clampedToExtent().applyingGaussianBlur(sigma: look.blur).cropped(to: ext) }
    if look.noise {
        let z = CIVector(x: 0, y: 0, z: 0, w: 0)
        let n = CIFilter(name: "CIRandomGenerator")!.outputImage!.cropped(to: ext).applyingFilter("CIColorMatrix", parameters: [
            "inputRVector": z, "inputGVector": z, "inputBVector": z, "inputAVector": CIVector(x: 0.12, y: 0, z: 0, w: 0)])
        ci = n.composited(over: ci)
    }
    if look.keystone > 0 {
        let k = ext.width * look.keystone
        ci = ci.applyingFilter("CIPerspectiveTransform", parameters: [
            "inputTopLeft": CIVector(x: k, y: ext.height), "inputTopRight": CIVector(x: ext.width - k, y: ext.height),
            "inputBottomLeft": CIVector(x: 0, y: 0), "inputBottomRight": CIVector(x: ext.width, y: 0)])
    }
    return ci.transformed(by: CGAffineTransform(scaleX: 0.8, y: 0.8))
}

let args = CommandLine.arguments
let (src, dst) = (args[1], args[2])
try? FileManager.default.createDirectory(atPath: dst, withIntermediateDirectories: true)
let ci = CIContext()
for (fixture, out, look) in looks {
    let text = try! String(contentsOfFile: "\(src)/\(fixture).txt", encoding: .utf8)
    let (f, title) = fields(text)
    let jpg = ci.jpegRepresentation(of: photo(paper(f, title), look), colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                                    options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): look.q])!
    try! jpg.write(to: URL(fileURLWithPath: "\(dst)/\(out).jpg"))
    print("\(out).jpg \(jpg.count / 1024) KB")
}
